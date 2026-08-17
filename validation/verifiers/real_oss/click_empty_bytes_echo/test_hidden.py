from io import BytesIO

import click


def test_empty_bytes_write_a_binary_newline():
    output = BytesIO()

    click.echo(b"", output)

    assert output.getvalue() == b"\n"
