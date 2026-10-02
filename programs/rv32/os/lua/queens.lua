-- queens.lua: count the ways to place N queens on an N by N board (Track 3, L2).
local n = tonumber(arg[1]) or 6
local cols, up, down = {}, {}, {}
local function place(row)
  if row > n then return 1 end
  local count = 0
  for c = 1, n do
    if not cols[c] and not up[row + c] and not down[row - c + n] then
      cols[c], up[row + c], down[row - c + n] = true, true, true
      count = count + place(row + 1)
      cols[c], up[row + c], down[row - c + n] = nil, nil, nil
    end
  end
  return count
end
print(n .. " queens: " .. place(1) .. " solutions")
