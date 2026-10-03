-- hello.lua: a script from the disk, run as `lua hello.lua NAME...` (Track 3, L2).
print("hello from " .. _VERSION .. " on the tiny computer")
print(#arg .. " arguments: " .. table.concat(arg, ", "))
local squares = {}
for i = 1, 8 do squares[#squares + 1] = i * i end
print("squares: " .. table.concat(squares, " "))
