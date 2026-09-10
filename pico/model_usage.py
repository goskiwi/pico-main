"""Usage observations shared by main, summary and memory requests."""


class ModelUsage:
    def __init__(self):
        self.reset()

    def reset(self):
        self.requests = self.responses = 0
        self.http_attempts = 0
        self.totals = dict.fromkeys(("input_tokens", "cached_tokens", "output_tokens"), 0)
        self.complete = True

    def snapshot(self):
        return {
            **self.totals,
            "model_requests": self.requests,
            "model_responses": self.responses,
            "http_attempts": self.http_attempts,
            "usage_complete": self.complete and self.requests == self.responses and self.responses > 0,
            "scope": "main, compaction and memory; delegate has separate usage",
        }


class MeteredClient:
    def __init__(self, client, usage):
        self.client, self.usage = client, usage

    def __getattr__(self, name):
        return getattr(self.client, name)

    def new_isolated_client(self):
        return MeteredClient(self.client.new_isolated_client(), self.usage)

    def complete_action(self, *args, **kwargs):
        self.usage.requests += 1
        self.client.request_attempts = 0
        try:
            action = self.client.complete_action(*args, **kwargs)
        except BaseException:
            self.usage.complete = False
            raise
        finally:
            attempts = self.client.request_attempts
            self.usage.http_attempts += attempts
            if attempts > 1:
                self.usage.complete = False
        self.usage.responses += 1
        metadata = self.client.last_completion_metadata
        for key in self.usage.totals:
            value = metadata.get(key)
            if type(value) is int:
                self.usage.totals[key] += value
            else:
                self.usage.complete = False
        return action
