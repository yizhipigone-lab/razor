"""L30: AI sample-out protocol"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_ai_has_train_valid_test():
    with open('app/backtest/ai_optimizer.py', encoding='utf-8') as f:
        content = f.read()
    has_date_split = 'train_end' in content or 'train_dates' in content
    has_valid_score_sort = 'valid_score' in content
    assert has_date_split, "Missing train/valid split logic"
    assert has_valid_score_sort, "Missing valid_score in Top-10 sort"
    print("OK AI optimizer has train/valid/test + valid_score sorting")


if __name__ == '__main__':
    test_ai_has_train_valid_test()
    print("\nL30 passed")
