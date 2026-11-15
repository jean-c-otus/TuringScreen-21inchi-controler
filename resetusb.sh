set -euo pipefail

target=""

for d in /sys/bus/usb/devices/*; do
    [[ -r "$d/idVendor" ]] || continue

    if [[ "$(cat "$d/idVendor")" == "1d6b" ]] &&
       [[ "$(cat "$d/idProduct")" == "0121" ]] &&
       [[ "$(cat "$d/serial" 2>/dev/null || true)" == "20080411" ]]; then
        target="$d"
        break
    fi
done

if [[ -z "$target" ]]; then
    echo "Display USB device not found"
    exit 1
fi

echo "Resetting display: $target"

echo 0 > "$target/authorized"
sleep 2
echo 1 > "$target/authorized"

echo "USB reauthorization completed"
