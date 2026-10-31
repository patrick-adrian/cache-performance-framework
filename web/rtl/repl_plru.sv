// Tree pseudo-LRU: WAYS-1 bits per set arranged as a binary tree, node 0 at the
// root. A clear bit sends the victim search down the left subtree. Touching a
// way points every node on its path at the other subtree, so the victim is
// always in the half of the tree less recently entered -- not the true LRU way,
// but one bit of state per way instead of a full ordering.

`default_nettype none

module repl_plru #(
  parameter int SETS = 64,
  parameter int WAYS = 4,

  localparam int IDX_W  = (SETS > 1) ? $clog2(SETS) : 1,
  localparam int WAY_W  = (WAYS > 1) ? $clog2(WAYS) : 1,
  localparam int NODES  = (WAYS > 1) ? WAYS - 1 : 1,
  localparam int LEVELS = (WAYS > 1) ? $clog2(WAYS) : 0
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [IDX_W-1:0] set_idx,
  input  logic             hit_upd,
  input  logic             fill_upd,
  input  logic [WAY_W-1:0] way_idx,
  output logic [WAY_W-1:0] victim_way
);

  logic [NODES-1:0] bits [SETS];
  logic             touch;

  assign touch = hit_upd | fill_upd;

  always_comb begin : plru_victim
    automatic int node = 0;
    for (int d = 0; d < LEVELS; d++) begin
      node = bits[set_idx][node] ? (2 * node + 2) : (2 * node + 1);
    end
    victim_way = WAY_W'(node - (WAYS - 1));
  end

  always_ff @(posedge clk) begin : plru_update
    automatic int node;
    automatic int parent;
    if (!rst_n) begin
      for (int s = 0; s < SETS; s++) begin
        bits[s] <= '0;
      end
    end else if (touch) begin
      node = int'(way_idx) + WAYS - 1;
      for (int d = 0; d < LEVELS; d++) begin
        parent = (node - 1) / 2;
        bits[set_idx][parent] <= (node == 2 * parent + 1);
        node = parent;
      end
    end
  end

endmodule

`default_nettype wire
