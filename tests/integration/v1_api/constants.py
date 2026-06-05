import uuid
from datetime import datetime

BASE_TEST_HEADER = {
    "x-client-id": str(uuid.uuid4()),
    "x-trace-id": str(uuid.uuid4()),
    "x-request-time": str(datetime.now()),
}
BASE_TEST_ENDPOINT = "/api/v1/{}"
