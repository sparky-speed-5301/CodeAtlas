class Base:
    pass


class Child(Base):
    pass


@app.route("/items")
def items():
    return os.getenv("TOKEN")
