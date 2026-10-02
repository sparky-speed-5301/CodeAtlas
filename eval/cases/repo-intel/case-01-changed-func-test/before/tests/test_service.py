from src.service import process_data

def test_process():
    assert process_data(1) == 1
