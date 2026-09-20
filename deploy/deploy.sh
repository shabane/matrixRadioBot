#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Source local deployment environment if exists
if [ -f "$DIR/.deploy.env" ]; then
    # shellcheck source=/dev/null
    source "$DIR/.deploy.env"
fi

NAMESPACE="${K8S_NAMESPACE:-default}"
SERVER="${DEPLOY_SERVER:-user@k8s-node}"

if [ "$SERVER" = "user@k8s-node" ]; then
    echo "ERROR: Please set DEPLOY_SERVER in .deploy.env or environment variable."
    exit 1
fi

K8S_MANIFEST="$DIR/deploy/kubernetes.yaml"
if [ -f "$DIR/deploy/kubernetes.local.yaml" ]; then
    K8S_MANIFEST="$DIR/deploy/kubernetes.local.yaml"
fi

echo "==> Syncing code and manifest ($K8S_MANIFEST) to remote server: $SERVER (namespace: $NAMESPACE)..."
ssh "$SERVER" "mkdir -p /tmp/matrixBot"
scp -q -r "$DIR/bot" "$DIR/config.yaml" "$DIR/requirements.txt" "$SERVER:/tmp/matrixBot/"
scp -q "$K8S_MANIFEST" "$SERVER:/tmp/matrixBot/kubernetes.yaml"

echo "==> Updating Kubernetes ConfigMaps and Deployment..."
ssh "$SERVER" "sudo k3s kubectl create configmap radio-bot-code --from-file=/tmp/matrixBot/bot -n $NAMESPACE --dry-run=client -o yaml | sudo k3s kubectl apply -f -"
ssh "$SERVER" "sudo k3s kubectl create configmap radio-bot-config --from-file=config.yaml=/tmp/matrixBot/config.yaml --from-file=requirements.txt=/tmp/matrixBot/requirements.txt -n $NAMESPACE --dry-run=client -o yaml | sudo k3s kubectl apply -f -"
ssh "$SERVER" "sudo k3s kubectl apply -f /tmp/matrixBot/kubernetes.yaml -n $NAMESPACE"
ssh "$SERVER" "sudo k3s kubectl rollout restart deployment/matrix-radio-bot -n $NAMESPACE"

echo "==> Deployment rollout restarted successfully!"
