"""
What a client needs in order to find a running broker.

The device layer already had this problem and solved it once: two
programs started independently cannot be handed ports and a shared secret
on a command line, so the facts get *published* instead
(:class:`~miainwoodpecker.devices.remote.AttachInvitation`). A broker has
the same shape and worse odds - its whole point is that the notebook, the
dashboard and the viewer are separate programs, possibly started by
different people - so it publishes the same way rather than inventing a
second convention per site.

One port, not a map of them. A device server binds a listener per target;
a broker multiplexes every target onto one connection, so there is one
number to publish and one to dial.
"""

from __future__ import annotations

import json
import pathlib
import stat
import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    import os

CONFIG_VERSION = 1
"""
Version stamped into a published invitation.

Refusing an unknown version beats guessing at it: the failure mode of
guessing is a client authenticating against nothing and an operator
watching a timeout with no idea why.
"""

DEFAULT_FILENAME = "broker.json"
"""The conventional name for a published invitation."""


@dataclass(frozen=True)
class BrokerInvitation:
    """
    Where a broker is listening, and the secret to present to it.

    Attributes
    ----------
    host : str
        The interface the broker bound. ``localhost`` keeps everything
        on the instrument PC, which is the right default for something
        that can move a probe.
    port : int
        The port it bound. Usually chosen by the OS, so it is not
        knowable in advance and has to be published.
    authkey : bytes
        The shared secret both ends pass to
        ``multiprocessing.connection``.
    """

    host: str
    port: int
    authkey: bytes

    def address(self) -> tuple[str, int]:
        """
        Return the ``(host, port)`` pair a client dials.

        Returns
        -------
        tuple[str, int]
            The address.
        """
        return (self.host, self.port)

    def as_config(self) -> dict[str, object]:
        """
        Return the invitation as plain JSON-compatible data.

        Returns
        -------
        dict[str, object]
            Versioned, so a future field can be added without an older
            client silently misreading the file.
        """
        return {
            "version": CONFIG_VERSION,
            "host": self.host,
            "port": self.port,
            "authkey": self.authkey.hex(),
        }

    @classmethod
    def from_config(
        cls,
        config: typing.Mapping[str, object],
    ) -> BrokerInvitation:
        """
        Rebuild an invitation from the data :meth:`as_config` produced.

        Parameters
        ----------
        config : typing.Mapping[str, object]
            Parsed configuration.

        Returns
        -------
        BrokerInvitation
            The invitation.

        Raises
        ------
        ValueError
            If the version is not one this code understands, or a
            required field is missing or malformed.
        """
        version = config.get("version")
        if version != CONFIG_VERSION:
            message = (
                f"broker invitation version {version!r} is not {CONFIG_VERSION}; "
                f"this file was written by a different version of miainwoodpecker"
            )
            raise ValueError(message)
        try:
            return cls(
                host=str(config["host"]),
                port=int(typing.cast("int", config["port"])),
                authkey=bytes.fromhex(str(config["authkey"])),
            )
        except (KeyError, TypeError) as error:
            message = f"broker invitation is missing or malformed: {error}"
            raise ValueError(message) from error

    def write_to(self, path: str | os.PathLike[str]) -> pathlib.Path:
        """
        Write the invitation to a file, readable only by this user.

        Parameters
        ----------
        path : str | os.PathLike[str]
            Where to write it. A directory gets
            :data:`DEFAULT_FILENAME` inside it.

        Returns
        -------
        pathlib.Path
            The path written.
        """
        destination = pathlib.Path(path)
        if destination.is_dir():
            destination = destination / DEFAULT_FILENAME
        destination.write_text(
            json.dumps(self.as_config(), indent=2) + "\n",
            encoding="utf-8",
        )
        # The authkey is in there, and an instrument-control PC is
        # usually a shared login. 0600 is not theatre here. It is a
        # no-op on Windows, where the file inherits the directory's ACL
        # instead - which is why the directory an operator publishes
        # into is their choice rather than a temp directory of ours.
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return destination

    @classmethod
    def read_from(cls, path: str | os.PathLike[str]) -> BrokerInvitation:
        """
        Read an invitation a broker published.

        Parameters
        ----------
        path : str | os.PathLike[str]
            The file, or a directory containing
            :data:`DEFAULT_FILENAME`.

        Returns
        -------
        BrokerInvitation
            The invitation.
        """
        source = pathlib.Path(path)
        if source.is_dir():
            source = source / DEFAULT_FILENAME
        return cls.from_config(json.loads(source.read_text(encoding="utf-8")))

    def describe(self) -> str:
        """
        Return a sentence an operator can act on.

        Returns
        -------
        str
            Where the broker is, without the secret in it - this goes to
            a terminal and a log, and the authkey belongs in neither.
        """
        return f"broker listening on {self.host}:{self.port}"
