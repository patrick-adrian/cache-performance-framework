// True LRU by per-way age counters: age 0 is most recently used, WAYS-1 least.
// Touching a way ages every way younger than it and resets it to 0, which keeps
// the ages a permutation of 0..WAYS-1 at all times.

`default_nettype none

module repl_lru #(
  parameter int SETS = 64,
  parameter int WAYS = 4,

  localparam int IDX_W = (SETS > 1) ? $clog2(SETS) : 1,
  localparam int WAY_W = (WAYS > 1) ? $clog2(WAYS) : 1
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [IDX_W-1:0] set_idx,
  input  logic             hit_upd,
  input  logic             fill_upd,
  input  logic [WAY_W-1:0] way_idx,
  output logic [WAY_W-1:0] victim_way
);

  logic [WAY_W-1:0] age [SETS][WAYS];
  logic             touch;

  assign touch = hit_upd | fill_upd;

  always_comb begin
    victim_way = '0;
    for (int w = 0; w < WAYS; w++) begin
      if (age[set_idx][w] == WAY_W'(WAYS - 1)) begin
        victim_way = WAY_W'(w);
      end
    end
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      for (int s = 0; s < SETS; s++) begin
        for (int w = 0; w < WAYS; w++) begin
          age[s][w] <= WAY_W'(w);
        end
      end
    end else if (touch) begin
      for (int w = 0; w < WAYS; w++) begin
        if (age[set_idx][w] < age[set_idx][way_idx]) begin
          age[set_idx][w] <= age[set_idx][w] + WAY_W'(1);
        end
      end
      age[set_idx][way_idx] <= '0;
    end
  end

endmodule

`default_nettype wire
