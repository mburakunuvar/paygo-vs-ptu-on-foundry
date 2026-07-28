#!/bin/bash

# Monitor benchmark progress every 15 minutes

RESULTS_DIR="/workspaces/foundry-skills/results/20260728T195619Z-4020a8f2"
LOG_FILE="/tmp/monitor.log"

echo "=== Benchmark Monitor Started ===" | tee -a "$LOG_FILE"
echo "Monitoring: $RESULTS_DIR" | tee -a "$LOG_FILE"
echo "Updates every 15 minutes" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

while true; do
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
  
  echo "[$TIMESTAMP] --- Status Check ---" | tee -a "$LOG_FILE"
  
  # Check if process is still running
  if pgrep -f "python app.py" > /dev/null; then
    echo "✓ Test process ACTIVE (PID: $(pgrep -f 'python app.py'))" | tee -a "$LOG_FILE"
  else
    echo "✗ Test process NOT RUNNING" | tee -a "$LOG_FILE"
  fi
  
  # Check aggregates size
  if [ -f "$RESULTS_DIR/aggregates.json" ]; then
    TRIALS=$(grep -c '"trial"' "$RESULTS_DIR/aggregates.json" 2>/dev/null || echo "0")
    FILE_SIZE=$(du -h "$RESULTS_DIR/aggregates.json" | cut -f1)
    LAST_MODIFIED=$(ls -lh "$RESULTS_DIR/aggregates.json" | awk '{print $6, $7, $8}')
    
    echo "Aggregates: $TRIALS trial results | Size: $FILE_SIZE | Last modified: $LAST_MODIFIED" | tee -a "$LOG_FILE"
  fi
  
  # Check requests file
  if [ -f "$RESULTS_DIR/requests.jsonl" ]; then
    REQ_COUNT=$(wc -l < "$RESULTS_DIR/requests.jsonl")
    echo "Total requests logged: $REQ_COUNT" | tee -a "$LOG_FILE"
  fi
  
  # Show last scenario from log
  if [ -f "/tmp/bench-run.log" ]; then
    LAST_SCENARIO=$(tail -20 /tmp/bench-run.log | grep -E "trial|global-standard|provisioned" | tail -1)
    echo "Last activity: $LAST_SCENARIO" | tee -a "$LOG_FILE"
  fi
  
  echo "" | tee -a "$LOG_FILE"
  
  # Sleep 15 minutes (900 seconds)
  sleep 900
done
