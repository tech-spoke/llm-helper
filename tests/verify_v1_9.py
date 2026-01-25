#!/usr/bin/env python3
"""
Simple verification script for v1.9 features (no pytest required)
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.session import Phase, SessionState


def test_batch_processing_method_exists():
    """Verify _index_files_batch method exists"""
    from tools.chromadb_manager import ChromaDBManager

    # Check method exists
    assert hasattr(ChromaDBManager, '_index_files_batch'), "_index_files_batch method not found"
    print("✓ ChromaDBManager._index_files_batch method exists")


def test_integrated_phase_exists():
    """Verify VERIFICATION_AND_IMPACT phase exists"""
    assert hasattr(Phase, 'VERIFICATION_AND_IMPACT'), "VERIFICATION_AND_IMPACT phase not found"
    print("✓ Phase.VERIFICATION_AND_IMPACT exists")


def test_integrated_method_exists():
    """Verify submit_verification_and_impact method exists"""
    session = SessionState(
        session_id="test",
        intent="IMPLEMENT",
        query="test",
        repo_path=Path("."),
    )

    assert hasattr(session, 'submit_verification_and_impact'), "submit_verification_and_impact method not found"
    print("✓ SessionState.submit_verification_and_impact method exists")


def test_phase_validation():
    """Verify phase validation works"""
    session = SessionState(
        session_id="test",
        intent="IMPLEMENT",
        query="test",
        repo_path=Path("."),
    )

    # Try to call in wrong phase
    result = session.submit_verification_and_impact(
        verified_hypotheses=[],
        verified_files=[],
    )

    assert result["success"] is False, "Should fail in wrong phase"
    print("✓ Phase validation works correctly")


def main():
    """Run all verification tests"""
    tests = [
        ("Batch processing method", test_batch_processing_method_exists),
        ("Integrated phase", test_integrated_phase_exists),
        ("Integrated method", test_integrated_method_exists),
        ("Phase validation", test_phase_validation),
    ]

    print("=" * 60)
    print("v1.9 Features Verification")
    print("=" * 60)

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {name}: Unexpected error: {e}")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
    else:
        print("\n✓ All v1.9 features verified successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
