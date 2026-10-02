from src.user import format_user
def test_empty():
    assert format_user('') == 'User: '
