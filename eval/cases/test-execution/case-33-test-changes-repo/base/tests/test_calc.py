def test_mutates():
    with open('unexpected_mutation.txt', 'w') as f:
        f.write('untracked file')
    assert True
