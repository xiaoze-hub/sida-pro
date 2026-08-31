"""单元测试: A 股短线情绪周期判别器"""

from src.core.sentiment_cycle import (
    classify_sentiment_cycle,
    format_cycle,
)


class TestClassifySentimentCycle:
    """classify_sentiment_cycle 核心测试"""

    def test_冰点_低涨停_低连板_高炸板(self):
        """冰点: 涨停少 + 连板低 + 炸板率高"""
        result = classify_sentiment_cycle({
            'limit_up_count': 20,
            'max_board_height': 2,
            'break_rate': 50.0,
            'yesterday_board_perf': -3.5,
            'losing_effect': 0.6,
        })
        assert result['cycle'] == '冰点'
        assert result['confidence'] in ('中', '高')
        assert len(result['hints']) > 0

    def test_修复_涨停回升_亏钱效应减弱(self):
        """修复: 涨停回升 + 炸板率可控 + 亏钱效应减弱"""
        result = classify_sentiment_cycle({
            'limit_up_count': 40,
            'max_board_height': 3,
            'break_rate': 28.0,
            'yesterday_board_perf': -0.5,
            'losing_effect': 0.2,
        })
        assert result['cycle'] == '修复'
        assert len(result['hints']) > 0

    def test_发酵_涨停增多_连板抬升(self):
        """发酵: 涨停增多 + 连板抬升 + 溢价为正"""
        result = classify_sentiment_cycle({
            'limit_up_count': 70,
            'max_board_height': 5,
            'break_rate': 22.0,
            'yesterday_board_perf': 2.5,
            'losing_effect': 0.15,
        })
        assert result['cycle'] == '发酵'
        assert len(result['hints']) > 0

    def test_高潮_涨停多_连板高_溢价足(self):
        """高潮: 涨停多 + 连板高 + 溢价充足"""
        result = classify_sentiment_cycle({
            'limit_up_count': 120,
            'max_board_height': 8,
            'break_rate': 30.0,
            'yesterday_board_perf': 4.0,
            'losing_effect': 0.1,
        })
        assert result['cycle'] == '高潮'
        assert result['confidence'] in ('中', '高')
        assert len(result['hints']) > 0

    def test_退潮_炸板高_高标炸板_亏钱扩散(self):
        """退潮: 炸板率高 + 连板降低 + 亏钱效应扩散"""
        result = classify_sentiment_cycle({
            'limit_up_count': 45,
            'max_board_height': 4,
            'break_rate': 45.0,
            'yesterday_board_perf': -2.5,
            'losing_effect': 0.6,
        })
        assert result['cycle'] == '退潮'
        assert len(result['hints']) > 0

    def test_全空数据降级(self):
        """全部字段为 None 应返回 '数据不足'"""
        result = classify_sentiment_cycle({
            'limit_up_count': None,
            'max_board_height': None,
            'break_rate': None,
            'yesterday_board_perf': None,
            'losing_effect': None,
        })
        assert result['cycle'] == '数据不足'
        assert result['confidence'] == '低'
        assert result['hints'] == []

    def test_空字典降级(self):
        """空字典应返回 '数据不足'"""
        result = classify_sentiment_cycle({})
        assert result['cycle'] == '数据不足'
        assert result['confidence'] == '低'

    def test_单指标缺失不崩(self):
        """仅 missing 一个指标不应返回数据不足"""
        result = classify_sentiment_cycle({
            'limit_up_count': 30,
            'max_board_height': 2,
            'break_rate': 50.0,
            'yesterday_board_perf': None,
            'losing_effect': None,
        })
        # 核心三个字段有值, 不应数据不足
        assert result['cycle'] != '数据不足'
        assert result['confidence'] in ('低', '中', '高')

    def test_两个核心缺失_仍可判断(self):
        """两个核心指标缺失仍可判断 (1个核心有值)"""
        result = classify_sentiment_cycle({
            'limit_up_count': 20,
            'max_board_height': None,
            'break_rate': None,
            'yesterday_board_perf': -3.5,
            'losing_effect': 0.6,
        })
        # 至少有一个核心字段有值, 不应数据不足 ... wait, 核心字段是 limit_up_count, max_board_height, break_rate
        # 这里 limit_up_count=20, 其他两个 None, 所以 core_missing=2 < 3, 可以计算
        # 但冰点规则需要 limit_up_count <= 30, 得 3 分, yesterday_board_perf <= -2 得 1 分, losing_effect >= 0.5 得 1 分
        # 共 5 分, 对比其他周期可能得分更低
        # 冰点满分 9, 5/9=0.56 >= 0.5, 中置信度
        assert result['cycle'] == '冰点'
        assert result['confidence'] in ('低', '中')

    def test_三项核心缺失_数据不足(self):
        """三个核心指标全缺失 -> 数据不足"""
        result = classify_sentiment_cycle({
            'limit_up_count': None,
            'max_board_height': None,
            'break_rate': None,
            'yesterday_board_perf': 2.0,
            'losing_effect': 0.1,
        })
        assert result['cycle'] == '数据不足'
        assert result['confidence'] == '低'

    def test_边界_冰点_修复_边缘(self):
        """冰点边缘: 刚好在冰点上限"""
        result = classify_sentiment_cycle({
            'limit_up_count': 30,
            'max_board_height': 3,
            'break_rate': 40.0,
            'yesterday_board_perf': -2.0,
            'losing_effect': 0.5,
        })
        # 冰点: limit_up_count <= 30(3分), board_height <= 3(2分), break_rate >= 40(2分), perf <= -2(1分), losing >= 0.5(1分) = 9分
        # 其他周期得分应更低
        assert result['cycle'] == '冰点'

    def test_边界_高潮_退潮_边缘(self):
        """高潮边缘转退潮: 炸板率已高, 溢价转负"""
        result = classify_sentiment_cycle({
            'limit_up_count': 100,
            'max_board_height': 7,
            'break_rate': 42.0,
            'yesterday_board_perf': -1.5,
            'losing_effect': 0.45,
        })
        # 高潮: limit_up_count >= 80(3分), board_height >= 6(2分), break_rate 20-45(1分) = 6分
        # 退潮: break_rate >= 35(3分), board_height <= 5? 7 > 5 不满足, yest_perf <= -1(2分), losing >= 0.4(2分) = 7分
        # 退潮得分更高
        assert result['cycle'] == '退潮'


class TestFormatCycle:
    """format_cycle 格式化测试"""

    def test_正常输出(self):
        result = classify_sentiment_cycle({
            'limit_up_count': 20,
            'max_board_height': 2,
            'break_rate': 50.0,
            'yesterday_board_perf': -3.5,
            'losing_effect': 0.6,
        })
        text = format_cycle(result)
        assert '短线情绪周期' in text
        assert '冰点' in text
        assert '操作提示' in text
        assert '置信度' in text

    def test_数据不足输出(self):
        result = classify_sentiment_cycle({})
        text = format_cycle(result)
        assert '数据不足' in text
        # 数据不足时 hints 为空, 不应有操作提示
        assert '操作提示' not in text

    def test_所有周期都能格式化(self):
        for metrics in [
            {'limit_up_count': 20, 'max_board_height': 2, 'break_rate': 50.0,
             'yesterday_board_perf': -3.5, 'losing_effect': 0.6},
            {'limit_up_count': 40, 'max_board_height': 3, 'break_rate': 28.0,
             'yesterday_board_perf': -0.5, 'losing_effect': 0.2},
            {'limit_up_count': 70, 'max_board_height': 5, 'break_rate': 22.0,
             'yesterday_board_perf': 2.5, 'losing_effect': 0.15},
            {'limit_up_count': 120, 'max_board_height': 8, 'break_rate': 30.0,
             'yesterday_board_perf': 4.0, 'losing_effect': 0.1},
            {'limit_up_count': 45, 'max_board_height': 4, 'break_rate': 45.0,
             'yesterday_board_perf': -2.5, 'losing_effect': 0.6},
        ]:
            result = classify_sentiment_cycle(metrics)
            text = format_cycle(result)
            assert result['cycle'] in text