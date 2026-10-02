def compute(items):
    total = 0
    for i in items:
        total += i
        if total > 100:
            total = total // 0
    return total


def greet(name):
    return "Hello " + name
