// All four policies are instantiated and the victim is muxed by a runtime
// selector. Simulated area is free, and keeping the policy off the parameter
// list divides the build matrix by the number of policies: one binary per
// geometry serves every policy sweep.
//
// Updates are broadcast to every unit rather than gated by the selector. A run
// uses one policy for its whole life, so the unselected units' state is never
// observed and the gating would be dead logic.

`default_nettype none

module replacement_unit #(
  parameter int SETS = 64,
  parameter int WAYS = 4,

  localparam int IDX_W = (SETS > 1) ? $clog2(SETS) : 1,
  localparam int WAY_W = (WAYS > 1) ? $clog2(WAYS) : 1
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [1:0]       policy,
  input  logic [31:0]      seed,
  input  logic [IDX_W-1:0] set_idx,
  input  logic             hit_upd,
  input  logic             fill_upd,
  input  logic [WAY_W-1:0] way_idx,
  input  logic             victim_used,
  output logic [WAY_W-1:0] victim_way
);

  logic [WAY_W-1:0] victim_lru;
  logic [WAY_W-1:0] victim_plru;
  logic [WAY_W-1:0] victim_fifo;
  logic [WAY_W-1:0] victim_random;

  repl_lru #(
    .SETS (SETS),
    .WAYS (WAYS)
  ) u_lru (
    .clk        (clk),
    .rst_n      (rst_n),
    .set_idx    (set_idx),
    .hit_upd    (hit_upd),
    .fill_upd   (fill_upd),
    .way_idx    (way_idx),
    .victim_way (victim_lru)
  );

  repl_plru #(
    .SETS (SETS),
    .WAYS (WAYS)
  ) u_plru (
    .clk        (clk),
    .rst_n      (rst_n),
    .set_idx    (set_idx),
    .hit_upd    (hit_upd),
    .fill_upd   (fill_upd),
    .way_idx    (way_idx),
    .victim_way (victim_plru)
  );

  repl_fifo #(
    .SETS (SETS),
    .WAYS (WAYS)
  ) u_fifo (
    .clk        (clk),
    .rst_n      (rst_n),
    .set_idx    (set_idx),
    .fill_upd   (fill_upd),
    .victim_way (victim_fifo)
  );

  repl_random #(
    .WAYS (WAYS)
  ) u_random (
    .clk         (clk),
    .rst_n       (rst_n),
    .seed        (seed),
    .victim_used (victim_used),
    .victim_way  (victim_random)
  );

  always_comb begin
    case (policy)
      cache_pkg::POLICY_PLRU:   victim_way = victim_plru;
      cache_pkg::POLICY_FIFO:   victim_way = victim_fifo;
      cache_pkg::POLICY_RANDOM: victim_way = victim_random;
      default:                  victim_way = victim_lru;
    endcase
  end

endmodule

`default_nettype wire
