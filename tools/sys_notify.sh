#!/usr/bin/env bash
set -euo pipefail

# @describe Send system status notification via Telegram

# Get disk usage
DISK_USAGE=$(df -h / | awk 'NR==2 {print $5}')

# Get battery level (requires termux-api)
BATTERY_LEVEL=$(termux-battery-status | grep -o '"percentage": [0-9]*' | awk '{print $2}')

# Build message
MESSAGE="System Status:
- Disk usage: $DISK_USAGE
- Battery: $BATTERY_LEVEL%"

# Send notification
"$HOME/.config/aichat/llm-functions/tools/notify.sh" --message "$MESSAGE"
