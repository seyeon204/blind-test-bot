class BlindTestBotError(Exception):
    pass


class SpecParseError(BlindTestBotError):
    pass


class TCGenerationError(BlindTestBotError):
    pass


class ExecutionError(BlindTestBotError):
    pass


class RunNotFoundError(BlindTestBotError):
    pass
