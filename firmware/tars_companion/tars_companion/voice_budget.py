"""Conservative session token accounting; missing usage stops daily mode."""
class VoiceBudget:
    def __init__(self, free_tokens, limit=20000):
        if free_tokens < 100000 or not 1000 <= limit <= 20000:
            raise ValueError('invalid_daily_token_budget')
        self.limit = min(limit, free_tokens-50000)
        self.used = 0
        self.round_seen = False

    def begin_turn(self): self.round_seen = False

    def observe(self, event):
        usage = event.get('usage') or event.get('response', {}).get('usage')
        if not isinstance(usage, dict): raise ValueError('usage_unavailable_daily_mode_paused')
        def count(value):
            if not isinstance(value, dict): return 0
            total = value.get('total_tokens')
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0: return total
            return sum((v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0)
                       if k.endswith('tokens') else count(v) for k, v in value.items())
        tokens = count(usage)
        if tokens <= 0: raise ValueError('usage_unavailable_daily_mode_paused')
        self.used += tokens
        self.round_seen = True

    @property
    def exhausted(self): return self.used >= self.limit
