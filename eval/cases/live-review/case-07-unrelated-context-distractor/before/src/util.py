def format_currency(amount):
    return f"${amount:,.2f}"


def slugify(text):
    return text.strip().lower().replace(" ", "-")
