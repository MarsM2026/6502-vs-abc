module adder2(input a0, input a1, input b0, input b1, input cin, output s0, output s1, output cout);
  wire g0 = b1 ^ a0;
  wire g1 = cin ^ b0;
  wire g2 = ~(cin ^ a0);
  wire g3 = a0 ^ a1;
  wire g4 = g3 ^ b1;
  wire g5 = ~(g2 | g1);
  wire g10 = g4 ^ g5;
  wire g11 = g4 & g5;
  wire g13 = ~g5;
  wire g14 = g13 & a0;
  wire g17 = ~(g14 ^ g11);
  wire g24 = g0 & g3;
  wire g50 = ~(g2 ^ b0);
  wire g91 = ~(g24 ^ g17);
  assign s0 = g50;
  assign s1 = g10;
  assign cout = g91;
endmodule
