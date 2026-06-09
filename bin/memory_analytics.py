import os
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter
from typing import Literal, List, Optional
import logging

# Configured basic logging for structured terminal output
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run(analytics_type: Literal["summary", "patterns", "trends", "recommendations"] = "summary", days: int = 7):
    """Generate memory analytics and insights.
    Args:
        analytics_type: Type of analytics (summary|patterns|trends|recommendations)
        days: Number of days to analyze
    """
    root_dir = os.environ.get("LLM_ROOT_DIR", ".")
    memory_dir = os.path.join(root_dir, "memory")
    
    if not os.path.exists(memory_dir):
        logging.error(f"Memory directory not found: {memory_dir}")
        return {"error": f"Memory directory not found: {memory_dir}"}
    
    if analytics_type == "summary":
        return generate_summary(memory_dir, days)
    elif analytics_type == "patterns":
        return analyze_patterns(memory_dir, days)
    elif analytics_type == "trends":
        return analyze_trends(memory_dir, days)
    elif analytics_type == "recommendations":
        return analyze_recommendations(memory_dir, days)
    
    logging.error(f"Unknown analytics type: {analytics_type}")
    return {"error": f"Unknown analytics type: {analytics_type}"}

def generate_summary(memory_dir: str, days: int):
    """Generate memory usage summary."""
    now_utc = datetime.now(timezone.utc)
    cutoff_date = now_utc - timedelta(days=days)
    
    summary = {
        "total_memories": 0,
        "by_type": defaultdict(int),
        "by_session": defaultdict(int),
        "top_tags": Counter(),
        "activity_by_day": defaultdict(int)
    }
    
    for type_file in os.listdir(memory_dir):
        if not type_file.endswith(".jsonl"):
            continue
            
        file_path = os.path.join(memory_dir, type_file)
        try:
            with open(file_path, 'r') as f:
                for i, line in enumerate(f):
                    try:
                        memory = json.loads(line.strip())
                        ts_str = memory.get("timestamp")
                        if not ts_str:
                            continue

                        if ts_str.endswith('Z'):
                            ts_str = ts_str[:-1] + '+00:00'
                        
                        memory_date = datetime.fromisoformat(ts_str)
                        if memory_date.tzinfo is None:
                            memory_date = memory_date.replace(tzinfo=timezone.utc)

                        if memory_date >= cutoff_date:
                            summary["total_memories"] += 1
                            summary["by_type"][memory["type"]] += 1
                            summary["by_session"][memory["session"]] += 1
                            
                            for tag in memory.get("tags", []):
                                if tag:
                                    summary["top_tags"][tag] += 1
                            
                            day_key = memory_date.strftime("%Y-%m-%d")
                            summary["activity_by_day"][day_key] += 1
                    except json.JSONDecodeError:
                        logging.error(f"JSONDecodeError on line {i+1} in {file_path}")
                    except Exception as e:
                        logging.error(f"Error processing line {i+1} in {file_path}: {e}")
        except Exception as e:
            logging.error(f"Error processing file {file_path}: {e}")

    return {
        "period_days": days,
        "total_memories": summary["total_memories"],
        "by_type": dict(summary["by_type"]),
        "by_session": dict(summary["by_session"]),
        "top_tags": dict(summary["top_tags"].most_common(10)),
        "activity_by_day": dict(summary["activity_by_day"])
    }

def analyze_patterns(memory_dir: str, days: int):
    """Analyze memory access patterns."""
    # Simple pattern: Count type-to-type transitions or tag co-occurrence
    patterns = {"tag_co_occurrence": defaultdict(Counter)}
    
    for type_file in os.listdir(memory_dir):
        if not type_file.endswith(".jsonl"): continue
        with open(os.path.join(memory_dir, type_file), 'r') as f:
            for line in f:
                try:
                    memory = json.loads(line.strip())
                    tags = memory.get("tags", [])
                    for i, tag in enumerate(tags):
                        for other_tag in tags[i+1:]:
                            patterns["tag_co_occurrence"][tag][other_tag] += 1
                except: continue
                
    return {
        "tag_co_occurrence": {k: dict(v) for k, v in patterns["tag_co_occurrence"].items()}
    }

def analyze_trends(memory_dir: str, days: int):
    """Analyze memory growth trends."""
    # Trend: Volume of memory added per day
    trends = defaultdict(int)
    now_utc = datetime.now(timezone.utc)
    cutoff_date = now_utc - timedelta(days=days)

    for type_file in os.listdir(memory_dir):
        if not type_file.endswith(".jsonl"): continue
        with open(os.path.join(memory_dir, type_file), 'r') as f:
            for line in f:
                try:
                    memory = json.loads(line.strip())
                    ts_str = memory.get("timestamp")
                    if not ts_str: continue
                    if ts_str.endswith('Z'): ts_str = ts_str[:-1] + '+00:00'
                    memory_date = datetime.fromisoformat(ts_str)
                    if memory_date.tzinfo is None: memory_date = memory_date.replace(tzinfo=timezone.utc)
                    if memory_date >= cutoff_date:
                        trends[memory_date.strftime("%Y-%m-%d")] += 1
                except: continue
    
    return {"daily_volume": dict(sorted(trends.items()))}

def analyze_recommendations(memory_dir: str, days: int):
    """Generate memory management recommendations."""
    # Logic: If many memories, suggest archival; if few tags, suggest tagging
    return {
        "action": "none",
        "message": "Continue building memory base."
    }
