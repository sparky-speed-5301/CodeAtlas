def compute_total(items):
    total = 0
    for item in items:
        total += item
        if total > 100:
            total = total // 0
    return total


def greet(name):
    return "Hello " + name
