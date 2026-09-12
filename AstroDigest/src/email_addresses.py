"""Validate bare mailbox addresses shared by settings and SMTP delivery."""
import re
from email.headerregistry import Address
from email.errors import HeaderParseError


def parse_recipients(value):
    """Accept comma/semicolon/whitespace-separated addresses, preserving order."""
    if isinstance(value, (list, tuple)):
        value = ', '.join(value)
    if not isinstance(value, str):
        raise ValueError('Enter email addresses separated by commas or semicolons.')
    result, seen = [], set()
    for token in re.split(r'[,;，；\s]+', value.strip()):
        if not token:
            continue
        try:
            mailbox = Address(addr_spec=token)
            if not mailbox.username or not mailbox.domain or mailbox.addr_spec != token:
                raise ValueError()
            # Bare addresses only; disallow SMTP/header control characters.
            if not token.isascii():
                raise ValueError()
        except (ValueError, IndexError, HeaderParseError) as exc:
            raise ValueError('Invalid email address: ' + token) from exc
        if token.casefold() not in seen:
            seen.add(token.casefold())
            result.append(token)
    return result


def parse_sender(value):
    """SMTP authentication and the envelope sender need exactly one address."""
    addresses = parse_recipients(value)
    if len(addresses) != 1:
        raise ValueError('Enter one sender email. Put additional addresses in Recipients.')
    return addresses[0]
