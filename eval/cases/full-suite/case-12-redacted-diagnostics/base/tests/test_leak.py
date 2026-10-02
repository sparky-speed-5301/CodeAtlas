def test_leak():
    token = 'ghp_' + '111122223333444455556666777788889999'
    raise ValueError(f'Leaked: {token}')
