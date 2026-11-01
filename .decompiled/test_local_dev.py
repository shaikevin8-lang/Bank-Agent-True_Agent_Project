import builtins as @py_builtins
import _pytest.assertion.rewrite as @pytest_ar
import socket
import sys
import pytest
from scripts.local_dev import check_ports, service_commands

@pytest.mark.parametrize('reuse_address', [False, True])
def test_occupied_port_rejected_without_terminating_its_owner(reuse_address):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, int(reuse_address))
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match=f'Port {port}'):
            check_ports({'mock-bank': port})
        @py_assert0 = listener.getsockname()[1]
        @py_assert2 = @py_assert0 == port
        if not @py_assert2:
            @py_format4 = @pytest_ar._call_reprcompare(('==',), (@py_assert2,), ('%(py1)s == %(py3)s',), (@py_assert0, port)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py3': @pytest_ar._saferepr(port) if 'port' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(port) else 'port'}
            @py_format6 = 'assert %(py5)s' % {'py5': @py_format4}
            raise AssertionError(@pytest_ar._format_explanation(@py_format6))
        @py_assert0 = @py_assert2 = None

def test_default_runtime_contains_only_three_local_processes():
    commands = service_commands()
    @py_assert2 = set(commands)
    @py_assert5 = {'api', 'web', 'mock-bank'}
    @py_assert4 = @py_assert2 == @py_assert5
    if not @py_assert4:
        @py_format7 = @pytest_ar._call_reprcompare(('==',), (@py_assert4,), ('%(py3)s\n{%(py3)s = %(py0)s(%(py1)s)\n} == %(py6)s',), (@py_assert2, @py_assert5)) % {'py0': @pytest_ar._saferepr(set) if 'set' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(set) else 'set', 'py1': @pytest_ar._saferepr(commands) if 'commands' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(commands) else 'commands', 'py3': @pytest_ar._saferepr(@py_assert2), 'py6': @pytest_ar._saferepr(@py_assert5)}
        @py_format9 = 'assert %(py8)s' % {'py8': @py_format7}
        raise AssertionError(@pytest_ar._format_explanation(@py_format9))
    @py_assert2 = @py_assert4 = @py_assert5 = None
    @py_assert0 = 'apps.mock_bank.main:create_app'
    @py_assert3 = commands['mock-bank']
    @py_assert2 = @py_assert0 in @py_assert3
    if not @py_assert2:
        @py_format5 = @pytest_ar._call_reprcompare(('in',), (@py_assert2,), ('%(py1)s in %(py4)s',), (@py_assert0, @py_assert3)) % {'py1': @pytest_ar._saferepr(@py_assert0), 'py4': @pytest_ar._saferepr(@py_assert3)}
        @py_format7 = 'assert %(py6)s' % {'py6': @py_format5}
        raise AssertionError(@pytest_ar._format_explanation(@py_format7))
    @py_assert0 = @py_assert2 = @py_assert3 = None

@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX TIME_WAIT port reuse')
def test_recently_closed_listener_is_available():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                connection.shutdown(socket.SHUT_WR)
                @py_assert1 = client.recv
                @py_assert3 = 1
                @py_assert5 = @py_assert1(@py_assert3)
                @py_assert8 = b''
                @py_assert7 = @py_assert5 == @py_assert8
                if not @py_assert7:
                    @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.recv\n}(%(py4)s)\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(client) if 'client' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(client) else 'client', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
                    @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
                    raise AssertionError(@pytest_ar._format_explanation(@py_format12))
                @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
                client.shutdown(socket.SHUT_WR)
                @py_assert1 = connection.recv
                @py_assert3 = 1
                @py_assert5 = @py_assert1(@py_assert3)
                @py_assert8 = b''
                @py_assert7 = @py_assert5 == @py_assert8
                if not @py_assert7:
                    @py_format10 = @pytest_ar._call_reprcompare(('==',), (@py_assert7,), ('%(py6)s\n{%(py6)s = %(py2)s\n{%(py2)s = %(py0)s.recv\n}(%(py4)s)\n} == %(py9)s',), (@py_assert5, @py_assert8)) % {'py0': @pytest_ar._saferepr(connection) if 'connection' in @py_builtins.locals() or @pytest_ar._should_repr_global_name(connection) else 'connection', 'py2': @pytest_ar._saferepr(@py_assert1), 'py4': @pytest_ar._saferepr(@py_assert3), 'py6': @pytest_ar._saferepr(@py_assert5), 'py9': @pytest_ar._saferepr(@py_assert8)}
                    @py_format12 = 'assert %(py11)s' % {'py11': @py_format10}
                    raise AssertionError(@pytest_ar._format_explanation(@py_format12))
                @py_assert1 = @py_assert3 = @py_assert5 = @py_assert7 = @py_assert8 = None
    check_ports({'recently-closed': port})

