// Encodings shared with the Python layer. These values are the wire format of
// the +POLICY and +WRITE_POLICY plusargs, so they must match the Policy and
// WritePolicy enums exactly.

`ifndef CACHE_PKG_SV
`define CACHE_PKG_SV

package cache_pkg;

  localparam logic [1:0] POLICY_LRU    = 2'd0;
  localparam logic [1:0] POLICY_PLRU   = 2'd1;
  localparam logic [1:0] POLICY_FIFO   = 2'd2;
  localparam logic [1:0] POLICY_RANDOM = 2'd3;

  localparam logic WRITE_BACK    = 1'b0;
  localparam logic WRITE_THROUGH = 1'b1;

  // Index width of a geometry with SETS sets. Zero sets is not a cache, and a
  // single set needs no index bits at all.
  function automatic int index_width(int sets);
    return (sets > 1) ? $clog2(sets) : 0;
  endfunction

  // Storage width for a field that may legitimately be zero bits wide. SystemVerilog
  // has no zero-width vector, so degenerate geometries carry one dummy bit.
  function automatic int storage_width(int bits);
    return (bits > 0) ? bits : 1;
  endfunction

endpackage

`endif
