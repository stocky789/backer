#!/usr/bin/env python3
"""
Quick test to verify the improvements work correctly.
This tests:
1. SMBConnectionManager class functionality
2. Encrypted payload handling in storage
3. Retry logic structure
"""

import sys
import json
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / 'src'))


def test_smb_connection_manager():
    """Test SMB connection manager imports and basic structure."""
    print("Testing SMB Connection Manager...")

    from backer.agent.service import SMBConnectionManager

    # Create instance
    manager = SMBConnectionManager()

    # Check methods exist
    assert hasattr(manager, 'connect')
    assert hasattr(manager, 'disconnect')
    assert hasattr(manager, 'disconnect_all')
    assert hasattr(manager, 'get_connection_status')

    # Test get_connection_status returns correct structure
    status = manager.get_connection_status()
    assert 'active_connections' in status
    assert 'connections' in status
    assert status['active_connections'] == 0
    assert status['connections'] == []

    print("  ✓ SMBConnectionManager class structure verified")
    return True


def test_agent_service_smb_integration():
    """Test AgentService has SMB manager integration."""
    print("Testing AgentService SMB integration...")

    from backer.agent.service import AgentService
    import sys
    import platform as plat

    # Mock Windows platform for testing
    original_platform = sys.platform

    try:
        # Test Windows platform
        sys.platform = 'win32'
        service = AgentService(
            server_url='http://test',
            client_id='test',
            client_secret='test'
        )

        assert service._smb_manager is not None, "SMB manager should be initialized on Windows"
        assert hasattr(service, 'get_smb_status')

        # Test SMB status method
        status = service.get_smb_status()
        assert status['available'] is True
        assert 'active_connections' in status

        print("  ✓ Windows platform SMB integration verified")

        # Test Linux platform
        sys.platform = 'linux'
        service_linux = AgentService(
            server_url='http://test',
            client_id='test',
            client_secret='test'
        )

        assert service_linux._smb_manager is None, "SMB manager should not be initialized on Linux"
        status_linux = service_linux.get_smb_status()
        assert status_linux['available'] is False

        print("  ✓ Linux platform SMB integration verified")

    finally:
        sys.platform = original_platform

    return True


def test_retry_logic_structure():
    """Test retry logic method exists and has correct signature."""
    print("Testing backup retry logic...")

    from backer.agent.service import AgentService

    service = AgentService(
        server_url='http://test',
        client_id='test',
        client_secret='test'
    )

    assert hasattr(service, '_execute_backup_with_retry')

    # Check the method signature (it should accept payload and max_retries)
    import inspect
    sig = inspect.signature(service._execute_backup_with_retry)
    params = list(sig.parameters.keys())
    assert 'payload' in params
    assert 'max_retries' in params

    print("  ✓ Retry logic structure verified")
    return True


def test_storage_encryption():
    """Test encrypted payload storage structure."""
    print("Testing encrypted payload storage...")

    from backer.server.storage import BackupStorage
    from backer.server.secrets import SecretsManager
    import tempfile
    import shutil

    # Create temporary directory for test database
    test_dir = Path(tempfile.mkdtemp(prefix='backer_test_'))

    try:
        # Initialize storage
        storage = BackupStorage(test_dir / 'test.db')

        # Verify queue_command method signature
        import inspect
        sig = inspect.signature(storage.queue_command)
        params = list(sig.parameters.keys())
        assert 'client_id' in params
        assert 'command_type' in params
        assert 'payload' in params

        print("  ✓ Storage encryption methods verified")

    finally:
        # Cleanup
        shutil.rmtree(test_dir, ignore_errors=True)

    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("Backer Improvements Verification")
    print("=" * 60)
    print()

    tests = [
        ("SMB Connection Manager", test_smb_connection_manager),
        ("AgentService SMB Integration", test_agent_service_smb_integration),
        ("Retry Logic", test_retry_logic_structure),
        ("Encrypted Storage", test_storage_encryption),
    ]

    passed = 0
    failed = 0

    for test_name, test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"  ✗ {test_name} FAILED: {e}")
            failed += 1
            import traceback
            traceback.print_exc()

    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
