def test_verbose():
    for i in range(500):
        print(f'Line {i}: ' + 'X' * 50)
    assert True
