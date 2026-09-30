// Round-robin insert pointer per set. There is no hit port: FIFO evicts in
// arrival order and a reference to a resident block does not change that order.

`default_nettype none

module repl_fifo #(
  parameter int SETS = 64,
  parameter int WAYS = 4,

  localparam int IDX_W = (SETS > 1) ? $clog2(SETS) : 1,
  localparam int WAY_W = (WAYS > 1) ? $clog2(WAYS) : 1
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [IDX_W-1:0] set_idx,
  input  logic             fill_upd,
  output logic [WAY_W-1:0] victim_way
);

  logic [WAY_W-1:0] ptr [SETS];

  assign victim_way = ptr[set_idx];

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      for (int s = 0; s < SETS; s++) begin
        ptr[s] <= '0;
      end
    end else if (fill_upd) begin
      // Explicit wrap rather than a truncating increment: at WAYS == 1 the
      // pointer is a single bit that must stay at zero.
      ptr[set_idx] <= (ptr[set_idx] == WAY_W'(WAYS - 1)) ? '0 : ptr[set_idx] + WAY_W'(1);
    end
  end

endmodule

`default_nettype wire
