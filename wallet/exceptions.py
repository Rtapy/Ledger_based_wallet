class WalletError(Exception):
    """Expected rejection; HTTP status mapping belongs to the API."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
