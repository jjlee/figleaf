a = 3; b = 0
while a:
    b += 1
    break
    b = 99
else:           #pragma: NO COVER
    b = 123
assert a == 3 and b == 1
