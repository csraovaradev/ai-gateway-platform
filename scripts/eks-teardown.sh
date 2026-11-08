#!/usr/bin/env bash
# Remove what the cluster created outside Terraform (ADR-0013), so `terraform destroy` can finish:
# the ALB, its target groups and security groups (made by the AWS Load Balancer Controller from the
# Ingresses), and the DNS records ExternalDNS added to the hosted zone.
#
# Then: terraform -chdir=terraform/eks destroy
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tf() { terraform -chdir="$ROOT/terraform/eks" output -raw "$1"; }
REGION="$(tf region)"
CLUSTER="$(tf cluster_name)"
DOMAIN="$(tf domain_name)"
CONTEXT="aigw-eks"
k() { kubectl --context "$CONTEXT" "$@"; }

if [ -z "$DOMAIN" ]; then
  echo "No domain_name set: nothing outside Terraform to remove."
  exit 0
fi

echo "==> deleting Ingresses (the controller then deletes the ALB)"
for ns in aigw auth; do
  k -n "$ns" delete ingress --all --ignore-not-found --wait=true >/dev/null 2>&1 || true
done

albs() {
  aws resourcegroupstaggingapi get-resources --region "$REGION" \
    --resource-type-filters elasticloadbalancing:loadbalancer \
    --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$CLUSTER" \
    --query 'length(ResourceTagMappingList)' --output text
}
for _ in $(seq 1 30); do
  [ "$(albs)" = 0 ] && break
  sleep 10
done
echo "    load balancers left for $CLUSTER: $(albs)"

echo "==> deleting DNS records ExternalDNS created in $DOMAIN"
ZONE="$(aws route53 list-hosted-zones-by-name --dns-name "$DOMAIN" \
  --query 'HostedZones[0].Id' --output text)"
CHANGES="$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE" --output json |
  python3 -c '
import json, sys
cluster, domain = sys.argv[1], sys.argv[2].rstrip(".") + "."
owned = f"external-dns/owner={cluster}"
records = json.load(sys.stdin)["ResourceRecordSets"]
# ExternalDNS marks each name it manages with a TXT record naming its owner; delete those names
# (A/AAAA aliases and the TXT markers), never anything else in the zone.
names = {r["Name"] for r in records if r["Type"] == "TXT"
         and any(owned in v["Value"] for v in r.get("ResourceRecords", []))}
def target(name):  # "a-api.example.com." marks "api.example.com." (ExternalDNS TXT format)
    label, rest = name.split(".", 1)
    for prefix in ("a-", "aaaa-", "cname-"):
        if label.startswith(prefix):
            return f"{label[len(prefix):]}.{rest}"
    return name
targets = {target(n) for n in names}
doomed = [r for r in records if r["Type"] in ("A", "AAAA", "CNAME", "TXT")
          and r["Name"] != domain and (r["Name"] in names or r["Name"] in targets)]
print(json.dumps({"Changes": [{"Action": "DELETE", "ResourceRecordSet": r} for r in doomed]}))
' "$CLUSTER" "$DOMAIN")"
COUNT="$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["Changes"]))' "$CHANGES")"
if [ "$COUNT" -gt 0 ]; then
  aws route53 change-resource-record-sets --hosted-zone-id "$ZONE" --change-batch "$CHANGES" >/dev/null
fi
echo "    removed $COUNT records"

cat <<EOF

Now run: terraform -chdir=terraform/eks destroy
The domain registration and its hosted zone stay (they are not part of the cluster).
EOF
