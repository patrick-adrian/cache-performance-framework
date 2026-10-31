// Fibonacci LFSR victim selection, x^32 + x^22 + x^2 + x + 1.
//
// The shift is clocked by victim_used -- one step per allocation into a full
// set -- and never by the bare clock. A cycle-driven LFSR would depend on the
// stall history of the run and so could not be reproduced by an untimed
// reference model, which would leave this policy unverifiable.

`default_nettype none

module repl_random #(
  parameter int WAYS = 4,

  localparam int WAY_W = (WAYS > 1) ? $clog2(WAYS) : 1
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [31:0]      seed,
  input  logic             victim_used,
  output logic [WAY_W-1:0] victim_way
);

  logic [31:0] state_q;
  logic [31:0] next_state;

  assign next_state = {state_q[30:0], state_q[31] ^ state_q[21] ^ state_q[1] ^ state_q[0]};

  // Advance-then-use: the way offered is the state the shift is about to land
  // on, so a consumer that pulses victim_used sees the same sequence as a model
  // that steps the LFSR inside its victim lookup.
  assign victim_way = (WAYS > 1) ? next_state[WAY_W-1:0] : '0;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      state_q <= seed;
    end else if (victim_used) begin
      state_q <= next_state;
    end
  end

endmodule

`default_nettype wire
