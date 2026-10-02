from src.user import format_user
def test_fmt():
    assert format_user('Alice') == 'User: Alice'
