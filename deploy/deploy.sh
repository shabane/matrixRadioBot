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

KCTL="sudo k3s kubectl"
LIVE_STATE_BACKUP="/tmp/matrixBot-livestate-backup"

echo "==> Syncing code and manifest ($K8S_MANIFEST) to remote server: $SERVER (namespace: $NAMESPACE)..."
ssh "$SERVER" "mkdir -p /tmp/matrixBot"
scp -q -r "$DIR/bot" "$DIR/config.yaml" "$DIR/requirements.txt" "$SERVER:/tmp/matrixBot/"
scp -q "$K8S_MANIFEST" "$SERVER:/tmp/matrixBot/kubernetes.yaml"

# The bot keeps its live per-room queue/current-song/loop state under /app/data/live_state
# so it can resume after a restart, but that directory isn't on a persistent volume -
# a rollout restart destroys the old pod's filesystem entirely. Pull it out of the old pod
# before restarting and push it into the new one once it's up, so the restart doesn't wipe
# whatever people are currently listening to. Best-effort throughout: if there's no old pod,
# no live state yet, or the copy fails for any reason, we just proceed with a normal deploy.
echo "==> Preserving in-flight playback queues across the restart (best effort)..."
ssh "$SERVER" "rm -rf $LIVE_STATE_BACKUP && mkdir -p $LIVE_STATE_BACKUP"
OLD_POD=$(ssh "$SERVER" "$KCTL get pods -n $NAMESPACE -l app=matrix-radio-bot -o jsonpath='{.items[0].metadata.name}' 2>/dev/null" || true)
if [ -n "$OLD_POD" ]; then
    if ssh "$SERVER" "$KCTL cp $NAMESPACE/$OLD_POD:/app/data/live_state $LIVE_STATE_BACKUP >/dev/null 2>&1"; then
        echo "    Backed up live state from pod $OLD_POD."
    else
        echo "    No in-flight queue state to preserve (nothing playing, or no previous pod)."
    fi
fi

echo "==> Updating Kubernetes ConfigMaps and Deployment..."
ssh "$SERVER" "$KCTL create configmap radio-bot-code --from-file=/tmp/matrixBot/bot -n $NAMESPACE --dry-run=client -o yaml | $KCTL apply -f -"
ssh "$SERVER" "$KCTL create configmap radio-bot-config --from-file=config.yaml=/tmp/matrixBot/config.yaml --from-file=requirements.txt=/tmp/matrixBot/requirements.txt -n $NAMESPACE --dry-run=client -o yaml | $KCTL apply -f -"
ssh "$SERVER" "$KCTL apply -f /tmp/matrixBot/kubernetes.yaml -n $NAMESPACE"
ssh "$SERVER" "$KCTL rollout restart deployment/matrix-radio-bot -n $NAMESPACE"

echo "==> Waiting for the new pod to come up..."
ssh "$SERVER" "$KCTL rollout status deployment/matrix-radio-bot -n $NAMESPACE --timeout=180s"

if ssh "$SERVER" "[ -n \"\$(ls -A $LIVE_STATE_BACKUP 2>/dev/null)\" ]"; then
    NEW_POD=$(ssh "$SERVER" "$KCTL get pods -n $NAMESPACE -l app=matrix-radio-bot -o jsonpath='{.items[0].metadata.name}'")
    echo "==> Restoring in-flight queue state into new pod $NEW_POD..."
    ssh "$SERVER" "$KCTL exec $NEW_POD -n $NAMESPACE -- mkdir -p /app/data/live_state"
    if ssh "$SERVER" "$KCTL cp $LIVE_STATE_BACKUP/. $NAMESPACE/$NEW_POD:/app/data/live_state/ >/dev/null 2>&1"; then
        echo "    Restored. The bot will resume each room's queue shortly after it finishes starting up."
    else
        echo "    WARNING: failed to copy live state into the new pod; affected rooms will need to !play again."
    fi
fi

echo "==> Deployment rollout restarted successfully!"
