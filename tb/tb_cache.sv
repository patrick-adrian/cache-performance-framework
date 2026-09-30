// Streams a trace through the cache and writes one CSV row of counters.
//
// The trace is read a line at a time rather than slurped with $readmemh, so
// trace length is not a build axis and a ten-million-reference run does not
// need a ten-million-entry array.
//
// Requests are driven on the falling edge and the cache samples them on the
// rising one, which keeps the two halves of the handshake in different halves
// of the clock period. Acceptance is observed through a registered flag rather
// than by reading req_ready after the edge: req_ready is combinational from the
// cache's own state, so by the time the falling edge arrives it already
// describes the next cycle, not the one that just consumed the request.

`default_nettype none

module tb_cache #(
  parameter int CACHE_SIZE_BYTES = 4096,
  parameter int BLOCK_SIZE_BYTES = 64,
  parameter int WAYS             = 4,
  parameter int ADDR_WIDTH       = 32
);

  logic clk   = 1'b0;
  logic rst_n = 1'b0;

  always #5 clk = ~clk;

  logic [1:0]  policy;
  logic        write_policy;
  logic        write_allocate;
  logic [31:0] miss_penalty;
  logic [31:0] hit_latency;
  logic [31:0] seed;

  logic                  req_valid;
  logic [ADDR_WIDTH-1:0] req_addr;
  logic                  req_write;
  logic                  req_ready;
  logic                  resp_valid;
  logic                  resp_hit;

  logic [63:0] stat_reads;
  logic [63:0] stat_writes;
  logic [63:0] stat_read_hits;
  logic [63:0] stat_write_hits;
  logic [63:0] stat_evictions;
  logic [63:0] stat_writebacks;
  logic [63:0] stat_memory_reads;
  logic [63:0] stat_memory_writes;
  logic [63:0] stat_cycles;

  cache #(
    .CACHE_SIZE_BYTES (CACHE_SIZE_BYTES),
    .BLOCK_SIZE_BYTES (BLOCK_SIZE_BYTES),
    .WAYS             (WAYS),
    .ADDR_WIDTH       (ADDR_WIDTH)
  ) u_dut (
    .clk                (clk),
    .rst_n              (rst_n),
    .policy             (policy),
    .write_policy       (write_policy),
    .write_allocate     (write_allocate),
    .miss_penalty       (miss_penalty),
    .hit_latency        (hit_latency),
    .seed               (seed),
    .req_valid          (req_valid),
    .req_addr           (req_addr),
    .req_write          (req_write),
    .req_ready          (req_ready),
    .resp_valid         (resp_valid),
    .resp_hit           (resp_hit),
    .stat_reads         (stat_reads),
    .stat_writes        (stat_writes),
    .stat_read_hits     (stat_read_hits),
    .stat_write_hits    (stat_write_hits),
    .stat_evictions     (stat_evictions),
    .stat_writebacks    (stat_writebacks),
    .stat_memory_reads  (stat_memory_reads),
    .stat_memory_writes (stat_memory_writes),
    .stat_cycles        (stat_cycles)
  );

  string trace_path;
  string csv_path;
  string dump_path;

  int trace_fd;
  int csv_fd;
  int dump_fd = 0;

  longint unsigned issued    = 0;
  longint unsigned responded = 0;

  logic [ADDR_WIDTH-1:0] nxt_addr;
  logic                  nxt_write;

  function automatic bit fetch_next();
    string       line;
    logic [63:0] word;
    while (1) begin
      if ($fgets(line, trace_fd) == 0) begin
        return 1'b0;
      end
      if ($sscanf(line, "%h", word) == 1) begin
        nxt_addr  = word[ADDR_WIDTH-1:0];
        nxt_write = word[32];
        return 1'b1;
      end
    end
  endfunction

  logic accepted;

  always @(posedge clk) begin
    accepted <= rst_n && req_valid && req_ready;

    if (rst_n && resp_valid) begin
      responded <= responded + 1;
      if (dump_fd != 0) begin
        $fwrite(dump_fd, "%0d", resp_hit ? 1 : 0);
      end
    end
  end

  initial begin : main
    int arg;

    if (ADDR_WIDTH > 32) begin
      $fatal(1, "tb_cache: the trace word format reserves bit 32 for the write flag");
    end

    if (!$value$plusargs("TRACE=%s", trace_path)) begin
      $fatal(1, "tb_cache: +TRACE=<path> is required");
    end
    if (!$value$plusargs("CSV=%s", csv_path)) begin
      $fatal(1, "tb_cache: +CSV=<path> is required");
    end
    if (!$value$plusargs("DUMP=%s", dump_path)) begin
      dump_path = "";
    end

    policy         = $value$plusargs("POLICY=%d", arg)         ? arg[1:0] : 2'd0;
    write_policy   = $value$plusargs("WRITE_POLICY=%d", arg)   ? arg[0]   : 1'b0;
    write_allocate = $value$plusargs("WRITE_ALLOCATE=%d", arg) ? arg[0]   : 1'b1;
    miss_penalty   = $value$plusargs("MISS_PENALTY=%d", arg)   ? arg       : 32'd20;
    hit_latency    = $value$plusargs("HIT_LATENCY=%d", arg)    ? arg       : 32'd1;
    seed           = $value$plusargs("SEED=%d", arg)           ? arg       : 32'd1;

    if (miss_penalty < 1) $fatal(1, "tb_cache: +MISS_PENALTY must be at least 1");
    if (hit_latency < 1)  $fatal(1, "tb_cache: +HIT_LATENCY must be at least 1");
    if (seed == 0)        $fatal(1, "tb_cache: +SEED must be non-zero, it is the LFSR lock-up state");

    trace_fd = $fopen(trace_path, "r");
    if (trace_fd == 0) $fatal(1, "tb_cache: cannot read trace %s", trace_path);
    if (dump_path != "") begin
      dump_fd = $fopen(dump_path, "w");
      if (dump_fd == 0) $fatal(1, "tb_cache: cannot write dump %s", dump_path);
    end

    req_valid = 1'b0;
    req_addr  = '0;
    req_write = 1'b0;

    repeat (4) @(negedge clk);
    rst_n = 1'b1;
    @(negedge clk);

    if (fetch_next()) begin
      req_addr  = nxt_addr;
      req_write = nxt_write;
      req_valid = 1'b1;
      issued    = 1;

      forever begin
        @(negedge clk);
        if (accepted) begin
          if (fetch_next()) begin
            req_addr  = nxt_addr;
            req_write = nxt_write;
            issued    = issued + 1;
          end else begin
            req_valid = 1'b0;
            break;
          end
        end
      end
    end

    while (responded < issued) begin
      @(negedge clk);
    end
    @(negedge clk);

    $fclose(trace_fd);
    if (dump_fd != 0) $fclose(dump_fd);

    if (responded != stat_reads + stat_writes) begin
      $fatal(1, "tb_cache: %0d responses for %0d accepted accesses",
             responded, stat_reads + stat_writes);
    end

    csv_fd = $fopen(csv_path, "w");
    if (csv_fd == 0) $fatal(1, "tb_cache: cannot write %s", csv_path);
    $fwrite(csv_fd,
            "accesses,reads,writes,hits,misses,read_hits,read_misses,",
            "write_hits,write_misses,evictions,writebacks,memory_reads,",
            "memory_writes,cycles\n");
    $fwrite(csv_fd, "%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d\n",
            stat_reads + stat_writes,
            stat_reads,
            stat_writes,
            stat_read_hits + stat_write_hits,
            stat_reads + stat_writes - stat_read_hits - stat_write_hits,
            stat_read_hits,
            stat_reads - stat_read_hits,
            stat_write_hits,
            stat_writes - stat_write_hits,
            stat_evictions,
            stat_writebacks,
            stat_memory_reads,
            stat_memory_writes,
            stat_cycles);
    $fclose(csv_fd);

    $finish;
  end

endmodule

`default_nettype wire
