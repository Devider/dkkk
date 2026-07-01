curl -X POST http://0.0.0.0:8888/api/v1/upload \
  -H "x-trace-id: $(uuidgen)" \
  -H "x-request-time: $(date -u +%Y-%m-%dT%H:%M:%S.000000+00:00)" \
  -F "file=@model.xlsx"
