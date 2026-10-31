// Fixed-latency backing store. ack rises exactly `latency` cycles after the
// cycle in which req was accepted, and a request presented in the same cycle as
// ack is accepted immediately -- without that, draining a dirty line before its
// refill would cost a wasted turnaround cycle and the measured miss penalty
// would not be the configured one.

`default_nettype none

module mem_stub (
  input  logic        clk,
  input  logic        rst_n,
  input  logic [31:0] latency,
  input  logic        req,
  output logic        ack
);

  logic [31:0] count_q;
  logic        busy_q;
  logic        start;

  assign ack   = busy_q && (count_q == 32'd0);
  assign start = req && (!busy_q || ack);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      busy_q  <= 1'b0;
      count_q <= '0;
    end else if (start) begin
      busy_q  <= 1'b1;
      count_q <= latency - 32'd1;
    end else if (ack) begin
      busy_q  <= 1'b0;
    end else if (busy_q) begin
      count_q <= count_q - 32'd1;
    end
  end

endmodule

`default_nettype wire
