// Blocking, single-outstanding-miss cache with a fixed-latency backing store.
//
// Geometry is elaboration-time because it fixes the array dimensions and the
// widths of the address fields. Everything else -- replacement policy, write
// policy, allocation on write, miss penalty, hit latency, LFSR seed -- is a
// runtime input, so one build covers the whole non-geometric sweep.
//
// Timing, per access:
//
//   hit                HIT_LATENCY
//   clean miss         HIT_LATENCY + MISS_PENALTY
//   dirty miss         HIT_LATENCY + 2 * MISS_PENALTY
//
// The dirty case is two full penalties because the victim drains before its
// replacement is fetched; there is no write buffer to overlap them.

`default_nettype none

module cache #(
  parameter int CACHE_SIZE_BYTES = 4096,
  parameter int BLOCK_SIZE_BYTES = 64,
  parameter int WAYS             = 4,
  parameter int ADDR_WIDTH       = 32,

  localparam int SETS     = CACHE_SIZE_BYTES / (BLOCK_SIZE_BYTES * WAYS),
  localparam int OFFSET_W = $clog2(BLOCK_SIZE_BYTES),
  localparam int INDEX_W  = cache_pkg::index_width(SETS),
  localparam int TAG_W    = ADDR_WIDTH - INDEX_W - OFFSET_W,
  localparam int IDX_W    = cache_pkg::storage_width(INDEX_W),
  localparam int WAY_W    = (WAYS > 1) ? $clog2(WAYS) : 1
) (
  input  logic                  clk,
  input  logic                  rst_n,

  // Runtime configuration. Sampled continuously but expected to be stable for
  // the whole run; the seed in particular is captured at reset.
  input  logic [1:0]            policy,
  input  logic                  write_policy,
  input  logic                  write_allocate,
  input  logic [31:0]           miss_penalty,
  input  logic [31:0]           hit_latency,
  input  logic [31:0]           seed,

  input  logic                  req_valid,
  input  logic [ADDR_WIDTH-1:0] req_addr,
  input  logic                  req_write,
  output logic                  req_ready,

  output logic                  resp_valid,
  output logic                  resp_hit,

  output logic [63:0]           stat_reads,
  output logic [63:0]           stat_writes,
  output logic [63:0]           stat_read_hits,
  output logic [63:0]           stat_write_hits,
  output logic [63:0]           stat_evictions,
  output logic [63:0]           stat_writebacks,
  output logic [63:0]           stat_memory_reads,
  output logic [63:0]           stat_memory_writes,
  output logic [63:0]           stat_cycles
);

  initial begin
    if (CACHE_SIZE_BYTES != SETS * BLOCK_SIZE_BYTES * WAYS) begin
      $fatal(1, "cache: %0d bytes is not a whole number of %0d-way sets of %0d bytes",
             CACHE_SIZE_BYTES, WAYS, BLOCK_SIZE_BYTES);
    end
    if ((WAYS & (WAYS - 1)) != 0) begin
      $fatal(1, "cache: WAYS must be a power of two, got %0d", WAYS);
    end
    if ((BLOCK_SIZE_BYTES & (BLOCK_SIZE_BYTES - 1)) != 0) begin
      $fatal(1, "cache: BLOCK_SIZE_BYTES must be a power of two, got %0d", BLOCK_SIZE_BYTES);
    end
    if (TAG_W < 1) begin
      $fatal(1, "cache: ADDR_WIDTH %0d leaves no tag bits", ADDR_WIDTH);
    end
  end

  typedef enum logic [1:0] {
    S_IDLE,  // accepting a request; a hit at HIT_LATENCY 1 completes here
    S_PRE,   // burning the remaining hit latency before the lookup resolves
    S_WB,    // draining a dirty victim
    S_FILL   // fetching the requested block
  } state_e;

  state_e state;

  logic [TAG_W-1:0] tag_mem   [SETS][WAYS];
  logic             valid_mem [SETS][WAYS];
  logic             dirty_mem [SETS][WAYS];

  logic [IDX_W-1:0] req_set;
  logic [TAG_W-1:0] req_tag;

  logic [IDX_W-1:0] set_q;
  logic [TAG_W-1:0] tag_q;
  logic             write_q;
  logic [WAY_W-1:0] way_q;
  logic             alloc_q;
  logic [31:0]      cnt_q;

  logic [IDX_W-1:0] cur_set;
  logic [TAG_W-1:0] cur_tag;
  logic             cur_write;

  logic             hit;
  logic [WAY_W-1:0] hit_way;
  logic             set_full;
  logic [WAY_W-1:0] free_way;
  logic [WAY_W-1:0] alloc_way;
  logic [WAY_W-1:0] repl_victim;

  logic             accept;
  logic             resolve_now;
  logic             allocating;
  logic             start_miss;
  logic             do_alloc;
  logic             do_evict;
  logic             do_wb;
  logic             fill_done;
  logic             write_back_mode;

  logic             mem_req;
  logic             mem_ack;

  logic [1:0]       mw_delta;

  // ---------------------------------------------------------------- decode

  if (INDEX_W > 0) begin : g_index
    assign req_set = req_addr[OFFSET_W +: INDEX_W];
  end else begin : g_no_index
    assign req_set = '0;
  end

  assign req_tag = req_addr[ADDR_WIDTH-1 -: TAG_W];

  assign cur_set   = (state == S_IDLE) ? req_set   : set_q;
  assign cur_tag   = (state == S_IDLE) ? req_tag   : tag_q;
  assign cur_write = (state == S_IDLE) ? req_write : write_q;

  assign write_back_mode = (write_policy == cache_pkg::WRITE_BACK);

  // ---------------------------------------------------------------- lookup

  always_comb begin
    hit     = 1'b0;
    hit_way = '0;
    for (int w = 0; w < WAYS; w++) begin
      if (valid_mem[cur_set][w] && (tag_mem[cur_set][w] == cur_tag)) begin
        hit     = 1'b1;
        hit_way = WAY_W'(w);
      end
    end
  end

  // Descending so that the lowest invalid way wins. A set with a free way never
  // consults the replacement policy, which is what keeps the policies
  // comparable: they only start to differ once the cache is warm.
  always_comb begin
    set_full = 1'b1;
    free_way = '0;
    for (int w = WAYS - 1; w >= 0; w--) begin
      if (!valid_mem[cur_set][w]) begin
        set_full = 1'b0;
        free_way = WAY_W'(w);
      end
    end
  end

  assign alloc_way = set_full ? repl_victim : free_way;

  // ------------------------------------------------------------- control

  assign req_ready   = (state == S_IDLE);
  assign accept      = (state == S_IDLE) && req_valid;

  assign resolve_now = (accept && (hit_latency == 32'd1))
                    || ((state == S_PRE) && (cnt_q == 32'd0));

  assign allocating  = !(cur_write && !write_allocate);
  assign start_miss  = resolve_now && !hit;
  assign do_alloc    = start_miss && allocating;
  assign do_evict    = do_alloc && set_full;
  assign do_wb       = do_evict && dirty_mem[cur_set][alloc_way];
  assign fill_done   = (state == S_FILL) && mem_ack;

  // Combinational so that the refill can be launched in the same cycle the
  // writeback is acknowledged.
  assign mem_req = start_miss || ((state == S_WB) && mem_ack);

  assign resp_valid = (resolve_now && hit) || fill_done;
  assign resp_hit   = (state != S_FILL);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      state   <= S_IDLE;
      cnt_q   <= '0;
      set_q   <= '0;
      tag_q   <= '0;
      write_q <= 1'b0;
      way_q   <= '0;
      alloc_q <= 1'b0;
    end else begin
      if (accept) begin
        set_q   <= req_set;
        tag_q   <= req_tag;
        write_q <= req_write;
      end

      case (state)
        S_IDLE: begin
          if (req_valid) begin
            if (hit_latency > 32'd1) begin
              state <= S_PRE;
              cnt_q <= hit_latency - 32'd2;
            end else if (!hit) begin
              way_q   <= alloc_way;
              alloc_q <= allocating;
              state   <= do_wb ? S_WB : S_FILL;
            end
          end
        end

        S_PRE: begin
          if (cnt_q != 32'd0) begin
            cnt_q <= cnt_q - 32'd1;
          end else if (hit) begin
            state <= S_IDLE;
          end else begin
            way_q   <= alloc_way;
            alloc_q <= allocating;
            state   <= do_wb ? S_WB : S_FILL;
          end
        end

        S_WB: begin
          if (mem_ack) begin
            state <= S_FILL;
          end
        end

        S_FILL: begin
          if (mem_ack) begin
            state <= S_IDLE;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  // ---------------------------------------------------------------- arrays

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      for (int s = 0; s < SETS; s++) begin
        for (int w = 0; w < WAYS; w++) begin
          valid_mem[s][w] <= 1'b0;
          dirty_mem[s][w] <= 1'b0;
          tag_mem[s][w]   <= '0;
        end
      end
    end else begin
      if (resolve_now && hit && cur_write && write_back_mode) begin
        dirty_mem[cur_set][hit_way] <= 1'b1;
      end
      if (fill_done && alloc_q) begin
        valid_mem[set_q][way_q] <= 1'b1;
        tag_mem[set_q][way_q]   <= tag_q;
        dirty_mem[set_q][way_q] <= write_q && write_back_mode;
      end
    end
  end

  // ------------------------------------------------------------ statistics

  // Several sources can charge a memory write in the same cycle, so they are
  // summed rather than written one at a time.
  always_comb begin
    mw_delta = 2'd0;
    if (resolve_now && hit && cur_write && !write_back_mode) begin
      mw_delta = mw_delta + 2'd1;
    end
    if (start_miss) begin
      if (!allocating) begin
        mw_delta = mw_delta + 2'd1;         // write-around: straight to memory
      end else if (cur_write && !write_back_mode) begin
        mw_delta = mw_delta + 2'd1;         // the store that rides the allocate
      end
      if (do_wb) begin
        mw_delta = mw_delta + 2'd1;
      end
    end
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      stat_reads         <= '0;
      stat_writes        <= '0;
      stat_read_hits     <= '0;
      stat_write_hits    <= '0;
      stat_evictions     <= '0;
      stat_writebacks    <= '0;
      stat_memory_reads  <= '0;
      stat_memory_writes <= '0;
      stat_cycles        <= '0;
    end else begin
      if ((state != S_IDLE) || req_valid) begin
        stat_cycles <= stat_cycles + 64'd1;
      end
      if (accept) begin
        if (req_write) begin
          stat_writes <= stat_writes + 64'd1;
        end else begin
          stat_reads <= stat_reads + 64'd1;
        end
      end
      if (resolve_now && hit) begin
        if (cur_write) begin
          stat_write_hits <= stat_write_hits + 64'd1;
        end else begin
          stat_read_hits <= stat_read_hits + 64'd1;
        end
      end
      if (do_alloc) begin
        stat_memory_reads <= stat_memory_reads + 64'd1;
      end
      if (do_evict) begin
        stat_evictions <= stat_evictions + 64'd1;
      end
      if (do_wb) begin
        stat_writebacks <= stat_writebacks + 64'd1;
      end
      stat_memory_writes <= stat_memory_writes + 64'(mw_delta);
    end
  end

  // ------------------------------------------------------------ submodules

  replacement_unit #(
    .SETS (SETS),
    .WAYS (WAYS)
  ) u_replacement (
    .clk         (clk),
    .rst_n       (rst_n),
    .policy      (policy),
    .seed        (seed),
    .set_idx     (cur_set),
    .hit_upd     (resolve_now && hit),
    .fill_upd    (fill_done && alloc_q),
    .way_idx     ((state == S_FILL) ? way_q : hit_way),
    .victim_used (do_evict),
    .victim_way  (repl_victim)
  );

  mem_stub u_mem (
    .clk     (clk),
    .rst_n   (rst_n),
    .latency (miss_penalty),
    .req     (mem_req),
    .ack     (mem_ack)
  );

endmodule

`default_nettype wire
