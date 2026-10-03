-- words.lua: the words of a file on the disk, counted and sorted (Track 3, L2).
local name = arg[1] or "welcome"
local file, err = io.open(name)
if not file then print(err) os.exit(1) end
local counts, total = {}, 0
for line in file:lines() do
  for word in line:lower():gmatch("%a+") do
    counts[word] = (counts[word] or 0) + 1
    total = total + 1
  end
end
file:close()
local words = {}
for word in pairs(counts) do words[#words + 1] = word end
table.sort(words, function(a, b) return counts[a] > counts[b] or counts[a] == counts[b] and a < b end)
print(string.format("%s: %d words, %d different", name, total, #words))
for i = 1, math.min(5, #words) do print(string.format("%3d %s", counts[words[i]], words[i])) end
local out = assert(io.open("words.out", "w"))
for _, word in ipairs(words) do out:write(word, " ", counts[word], "\n") end
out:close()
