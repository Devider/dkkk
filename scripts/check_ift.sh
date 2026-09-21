curl --location --request POST 'https://pipeline-impl.apps.a13ccft0.k8s.delta.sbrf.ru/pipeline/v1/cashflow/copilot/574e6111-31e6-4e4d-8584-68f2245d4442/file' \
--header 'X-USER: 01924968' \
--form 'file=@"/Users/19764852/Downloads/Methanex_FinModel.xlsx"'


curl --location --request POST 'https://pipeline-impl.apps.a13ccft0.k8s.delta.sbrf.ru/pipeline/v1/cashflow/copilot/310e974b-73a0-4a2f-8e3c-25c7dfbf02f5/message' \
--header 'X-USER: 01924968' \
--header 'Content-Type: application/json' \
--data-raw '{
"messageRequest": "{сюда вставить сообщение}"
}'