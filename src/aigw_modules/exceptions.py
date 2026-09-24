class HealthCheckError(Exception):
    """
    Исключение, возникающее при неудачной проверке состояния интерфейса.

    Attributes:
        http_url (str): URL, по которому проводилась проверка.
        exc (Exception):  Исключение, возникшее во время проверки.
    """

    def __init__(self, http_url, exc) -> None:
        super().__init__(f"Health check for {http_url} wasn't successful: {exc}")
