# Windows 터미널에서 시연 (한글이 깨지지 않게 UTF-8 로 바꾼 뒤 VM 의 demo.sh 실행)
chcp 65001 | Out-Null
[Console]::OutputEncoding = [Text.Encoding]::UTF8
gcloud compute ssh nw-lab --project=kt-nw-lab-1027 --zone=asia-northeast3-a --quiet --ssh-flag=-t --command="sh /opt/nwlab/demo.sh"
