#!/usr/bin/env python3
"""
Trinity Platform - Automated Integration Test Suite

This script runs a comprehensive integration test of the Trinity platform,
validating agent lifecycle, chat, state persistence, and agent-to-agent collaboration.

Usage:
    python3 docs/testing/run_integration_test.py

Prerequisites:
    - Backend running at http://localhost:8000
    - Frontend running at http://localhost:3000
    - Docker daemon running
    - Test templates available (github:abilityai/test-agent-*)

Author: Trinity Test Suite
Version: 1.0.0
Last Updated: 2025-12-08
"""

import requests
import time
import sys
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000"
ADMIN_USER = "admin"
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "changeme")

# Test agents used
TEST_AGENTS = ["test-echo", "test-counter", "test-delegator"]

# Timing configuration
AGENT_INIT_WAIT = 10  # Seconds to wait after agent starts for initialization
CHAT_RETRY_WAIT = 5   # Seconds between chat retries on 503
MAX_CHAT_RETRIES = 5  # Maximum retries for chat
CHAT_TIMEOUT = 180    # Timeout for chat requests

# Test results
results = {
    "passed": 0,
    "failed": 0,
    "warnings": 0,
    "tests": []
}


def log(message, level="INFO"):
    """Print timestamped log message"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": " ", "PASS": "✓", "FAIL": "✗", "WARN": "⚠"}
    print(f"[{timestamp}] {prefix.get(level, ' ')} {message}")


def get_token():
    """Get authentication token"""
    r = requests.post(f"{BASE_URL}/api/token",
        data={"username": ADMIN_USER, "password": ADMIN_PASS})
    if r.status_code != 200:
        raise Exception(f"Failed to get token: {r.status_code}")
    return r.json()["access_token"]


def send_chat(headers, agent_name, message, max_retries=MAX_CHAT_RETRIES):
    """Send chat with retry logic for 503 errors"""
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{BASE_URL}/api/agents/{agent_name}/chat",
                headers=headers,
                json={"message": message},
                timeout=CHAT_TIMEOUT)

            if r.status_code == 200:
                return {"success": True, "response": r.json().get("response", "")}
            elif r.status_code == 503:
                log(f"Agent {agent_name} not ready (503), retry {attempt+1}/{max_retries}", "WARN")
                time.sleep(CHAT_RETRY_WAIT)
            else:
                return {"success": False, "error": f"{r.status_code}: {r.text[:200]}"}
        except requests.exceptions.Timeout:
            log(f"Chat timeout, retry {attempt+1}/{max_retries}", "WARN")
            time.sleep(CHAT_RETRY_WAIT)

    return {"success": False, "error": "Max retries exceeded"}


def wait_for_agent(headers, agent_name, target_status="running", max_wait=60):
    """Wait for agent to reach target status"""
    waited = 0
    while waited < max_wait:
        r = requests.get(f"{BASE_URL}/api/agents/{agent_name}", headers=headers)
        if r.status_code == 200:
            status = r.json().get("status")
            if status == target_status:
                return True
        time.sleep(2)
        waited += 2
    return False


def record_test(name, passed, details=""):
    """Record test result"""
    if passed:
        results["passed"] += 1
        log(f"{name}: PASSED", "PASS")
    else:
        results["failed"] += 1
        log(f"{name}: FAILED - {details}", "FAIL")
    results["tests"].append({"name": name, "passed": passed, "details": details})


def cleanup(headers):
    """Clean up test agents"""
    log("Cleaning up test agents...")
    agents = requests.get(f"{BASE_URL}/api/agents", headers=headers).json()
    for a in agents:
        if a["name"] in TEST_AGENTS:
            requests.delete(f"{BASE_URL}/api/agents/{a['name']}", headers=headers)
            log(f"  Deleted {a['name']}")


# ============================================================
# TEST FUNCTIONS
# ============================================================

def test_prerequisites(headers):
    """Test 0: Verify prerequisites"""
    log("=" * 60)
    log("TEST 0: Prerequisites")
    log("=" * 60)

    # Check backend
    r = requests.get(f"{BASE_URL}/health", timeout=5)
    record_test("Backend health", r.status_code == 200)

    # Check templates
    r = requests.get(f"{BASE_URL}/api/templates", headers=headers)
    templates = r.json()
    test_templates = [t for t in templates if "test-agent-" in t.get("id", "")]
    record_test("Test templates available", len(test_templates) >= 3,
                f"Found {len(test_templates)}/8")


def test_agent_creation(headers):
    """Test 1: Agent Creation"""
    log("=" * 60)
    log("TEST 1: Agent Creation (test-echo)")
    log("=" * 60)

    # Create agent
    start = time.time()
    r = requests.post(f"{BASE_URL}/api/agents",
        headers=headers,
        json={"name": "test-echo", "template": "github:abilityai/test-agent-echo"})

    record_test("Create test-echo", r.status_code in [200, 201])

    # Wait for running
    running = wait_for_agent(headers, "test-echo", "running")
    record_test("Agent starts running", running)

    # Extra initialization time
    log(f"Waiting {AGENT_INIT_WAIT}s for agent initialization...")
    time.sleep(AGENT_INIT_WAIT)


def test_basic_chat(headers):
    """Test 2: Basic Chat"""
    log("=" * 60)
    log("TEST 2: Basic Chat (test-echo)")
    log("=" * 60)

    # Send message
    result = send_chat(headers, "test-echo", "Hello World")
    record_test("Chat API responds", result["success"])

    if result["success"]:
        response = result["response"]
        record_test("Response contains 'Echo'", "echo" in response.lower())
        record_test("Response contains 'Hello World'", "hello world" in response.lower())


def test_state_persistence(headers):
    """Test 3: State Persistence"""
    log("=" * 60)
    log("TEST 3: State Persistence (test-counter)")
    log("=" * 60)

    # Create counter agent
    r = requests.post(f"{BASE_URL}/api/agents",
        headers=headers,
        json={"name": "test-counter", "template": "github:abilityai/test-agent-counter"})
    record_test("Create test-counter", r.status_code in [200, 201])

    # Wait for running
    running = wait_for_agent(headers, "test-counter", "running")
    record_test("Counter agent starts", running)

    log(f"Waiting {AGENT_INIT_WAIT}s for agent initialization...")
    time.sleep(AGENT_INIT_WAIT)

    # Test counter operations
    result = send_chat(headers, "test-counter", "reset")
    record_test("Reset counter", result["success"] and "0" in result.get("response", ""))

    result = send_chat(headers, "test-counter", "increment")
    record_test("Increment counter", result["success"] and "1" in result.get("response", ""))

    result = send_chat(headers, "test-counter", "add 10")
    record_test("Add 10 to counter", result["success"] and "11" in result.get("response", ""))


def test_agent_collaboration(headers):
    """Test 4: Agent-to-Agent Collaboration"""
    log("=" * 60)
    log("TEST 4: Agent-to-Agent Collaboration (test-delegator)")
    log("=" * 60)

    # Create delegator agent
    r = requests.post(f"{BASE_URL}/api/agents",
        headers=headers,
        json={"name": "test-delegator", "template": "github:abilityai/test-agent-delegator"})
    record_test("Create test-delegator", r.status_code in [200, 201])

    # Wait for running
    running = wait_for_agent(headers, "test-delegator", "running")
    record_test("Delegator agent starts", running)

    # Extra wait for MCP server initialization
    log(f"Waiting {AGENT_INIT_WAIT + 5}s for MCP server initialization...")
    time.sleep(AGENT_INIT_WAIT + 5)

    # Test list agents
    result = send_chat(headers, "test-delegator", "list agents")
    if result["success"]:
        response = result["response"].lower()
        record_test("List agents",
                   "test-echo" in response and "test-counter" in response)
    else:
        record_test("List agents", False, result.get("error", ""))

    # Test delegation
    start = time.time()
    result = send_chat(headers, "test-delegator", "delegate to test-echo: ping from test")
    elapsed = time.time() - start

    if result["success"]:
        response = result["response"].lower()
        record_test(f"Delegation (took {elapsed:.1f}s)",
                   "echo" in response or "ping" in response)
    else:
        record_test("Delegation", False, result.get("error", ""))


def test_dashboard_apis(headers):
    """Test 5: Dashboard APIs"""
    log("=" * 60)
    log("TEST 5: Dashboard APIs")
    log("=" * 60)

    # Context stats
    r = requests.get(f"{BASE_URL}/api/agents/context-stats", headers=headers)
    if r.status_code == 200:
        data = r.json()
        agents_count = len(data.get("agents", []))
        record_test("Context stats API", agents_count >= 3, f"{agents_count} agents")
    else:
        record_test("Context stats API", False, f"Status {r.status_code}")

    # Activity timeline
    r = requests.get(f"{BASE_URL}/api/activities/timeline?limit=20", headers=headers)
    if r.status_code == 200:
        data = r.json()
        count = data.get("count", 0)
        record_test("Activity timeline API", count > 0, f"{count} activities")
    else:
        record_test("Activity timeline API", False, f"Status {r.status_code}")

    # Plans aggregate
    r = requests.get(f"{BASE_URL}/api/agents/plans/aggregate", headers=headers)
    record_test("Plans aggregate API", r.status_code == 200)


def test_agent_lifecycle(headers):
    """Test 6: Agent Lifecycle"""
    log("=" * 60)
    log("TEST 6: Agent Lifecycle")
    log("=" * 60)

    # Stop agent
    r = requests.post(f"{BASE_URL}/api/agents/test-echo/stop", headers=headers)
    record_test("Stop agent", r.status_code == 200)

    time.sleep(3)
    stopped = wait_for_agent(headers, "test-echo", "stopped", max_wait=30)
    record_test("Agent stopped", stopped)

    # Start agent
    r = requests.post(f"{BASE_URL}/api/agents/test-echo/start", headers=headers)
    record_test("Start agent", r.status_code == 200)

    running = wait_for_agent(headers, "test-echo", "running")
    record_test("Agent restarted", running)

    # Chat after restart
    time.sleep(AGENT_INIT_WAIT)
    result = send_chat(headers, "test-echo", "post-restart test")
    record_test("Chat after restart", result["success"])

    # Delete agent
    r = requests.delete(f"{BASE_URL}/api/agents/test-echo", headers=headers)
    record_test("Delete agent", r.status_code == 200)

    time.sleep(2)
    r = requests.get(f"{BASE_URL}/api/agents/test-echo", headers=headers)
    record_test("Agent deleted (404)", r.status_code == 404)


# ============================================================
# MAIN
# ============================================================

def main():
    start_time = time.time()

    log("=" * 60)
    log("TRINITY PLATFORM - INTEGRATION TEST SUITE")
    log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    try:
        # Get auth token
        log("Authenticating...")
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        log("Authentication successful")

        # Clean slate
        cleanup(headers)

        # Run tests
        test_prerequisites(headers)
        test_agent_creation(headers)
        test_basic_chat(headers)
        test_state_persistence(headers)
        test_agent_collaboration(headers)
        test_dashboard_apis(headers)
        test_agent_lifecycle(headers)

    except Exception as e:
        log(f"FATAL ERROR: {e}", "FAIL")
        results["failed"] += 1
    finally:
        # Cleanup
        try:
            token = get_token()
            headers = {"Authorization": f"Bearer {token}"}
            cleanup(headers)
        except:
            pass

    # Summary
    elapsed = time.time() - start_time
    log("")
    log("=" * 60)
    log("TEST SUMMARY")
    log("=" * 60)
    log(f"Total tests:  {results['passed'] + results['failed']}")
    log(f"Passed:       {results['passed']}", "PASS" if results["passed"] else "INFO")
    log(f"Failed:       {results['failed']}", "FAIL" if results["failed"] else "INFO")
    log(f"Duration:     {elapsed:.1f}s")
    log("")

    if results["failed"] > 0:
        log("FAILED TESTS:")
        for test in results["tests"]:
            if not test["passed"]:
                log(f"  - {test['name']}: {test['details']}", "FAIL")

    log("=" * 60)
    overall = "PASS" if results["failed"] == 0 else "FAIL"
    log(f"OVERALL RESULT: {overall}", overall)
    log("=" * 60)

    return 0 if results["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
