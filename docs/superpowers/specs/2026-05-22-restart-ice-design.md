# restart_ice() — design

Tracks [issue #14](https://github.com/social-home-io/aiolibdatachannel/issues/14):
recover from `ICEState.FAILED` without tearing down the whole
`PeerConnection`. Today the only recovery path is a full PC rebuild,
which also drops the DTLS / SCTP layers (and every open DataChannel)
above. The W3C-spec way is an ICE restart: re-run ICE on the existing
PC while leaving DTLS and SCTP intact.

## Public API

Add one method on `PeerConnection`:

```python
async def restart_ice(self, *, trickle: bool = False) -> LocalDescription:
    """Re-run ICE without dropping the existing PeerConnection.

    ``trickle=False`` (the default) mirrors :meth:`create_offer`:
    awaits the new ICE gathering cycle and returns the complete
    inline-ICE SDP. Hand the result to the remote peer over your
    signalling channel; the remote then answers and you feed the
    answer back via :meth:`set_remote_description`.

    ``trickle=True`` mirrors :meth:`set_local_description`: returns
    the bare SDP as soon as libdatachannel produces it. Iterate
    :meth:`ice_candidates` to forward new candidates as they are
    discovered.

    Either mode: libdatachannel decides whether the call is a true
    ICE restart (PC previously connected) or a fresh-offer build
    (PC never connected). The wrapper does not gate by ICE / RTC
    state; the only Python-side pre-condition is that ``close`` /
    ``aclose`` has not run.

    :raises ConnectionClosedError: if ``close`` / ``aclose`` already ran.
    :raises RTCError: ``trickle=False`` and the native layer fails
        to produce an SDP after gathering completes (shouldn't
        happen on a healthy PC).
    """
```

Also document the existing `set_local_description(type_=None)` path
as the W3C-spec alias for `restart_ice(trickle=True)`. Per W3C
WebRTC §setLocalDescription: *"If type is missing and the connection
has been previously connected, the user agent MUST do an ICE
restart."* The two entry points are functionally identical post-fix
(see *Implementation* below).

## Implementation

### `restart_ice()`

Inside `aiolibdatachannel/peer_connection.py`, next to `create_offer`:

```python
async def restart_ice(self, *, trickle: bool = False) -> LocalDescription:
    if self._closed:
        raise ConnectionClosedError("peer connection is closed")
    self._local_description.reset()
    if not trickle:
        self._gathering_complete.reset()
    while not self._ice_candidates.empty():
        self._ice_candidates.get_nowait()
    self._native.set_local_description("offer")
    if trickle:
        return await self._local_description.future
    await self._gathering_complete.future
    sdp = self._native.get_local_description()
    kind = _coerce_sdp_type(
        self._native.get_local_description_type(), default="offer"
    )
    if sdp is None:
        raise RTCError("local description not available after gathering")
    return LocalDescription(sdp=sdp, type=kind)
```

Differences from `create_offer`:

1. **`trickle` kwarg.** When `True`, skip the gathering-complete
   reset and return as soon as the local-description slot resolves.
   The caller pulls candidates via `ice_candidates()`.
2. **Queue drain.** `create_offer` was called when `_ice_candidates`
   was guaranteed empty. On restart, stale items (typically the
   `None` sentinel pushed by the previous gather-complete) may
   still sit in the queue; drain them so `ice_candidates()`
   consumers after the restart see a clean stream that terminates
   only on the new gather-complete.

### `set_local_description(type_=None)` becomes the trickle alias

Extend the existing `set_local_description` to perform the same
bookkeeping when called repeatedly on a non-fresh PC:

```python
async def set_local_description(
    self, type_: SdpType | None = None
) -> LocalDescription:
    if self._closed:
        raise ConnectionClosedError("peer connection is closed")
    self._local_description.reset()
    while not self._ice_candidates.empty():
        self._ice_candidates.get_nowait()
    self._native.set_local_description(type_)
    return await self._local_description.future
```

The first-call behaviour is unchanged: the queue is empty on a
fresh PC, so the drain is a no-op. On a subsequent call (the W3C
ICE-restart alias case), the drain protects `ice_candidates()`
consumers exactly as `restart_ice(trickle=True)` does. After the
change, the two entry points are equivalent for the
`type_=None` form:

```python
await pc.restart_ice(trickle=True)
# is exactly the same as
await pc.set_local_description(None)
```

`restart_ice` is the recommended entry — its name documents intent.
`set_local_description(None)` is kept as the spec alias for callers
who prefer the W3C-shaped API.

No changes to `_handle_gathering_state` or `_iter_ice_candidates`:
the existing slot-reset + sentinel-on-complete logic already
generalises across multiple `set_local_description("offer")` calls
once the queue is drained.

## Tests

Three new tests in `tests/test_restart_ice.py`.

### Fake-based, gathered form (no `@pytest.mark.native`)

Drives the fake `_native` directly:

1. Create PC.
2. `create_offer()` task — manually emit gathering-complete via
   `emit(CB_GATHERING_STATE_CHANGE, pc_handle, COMPLETE)`. Assert
   the awaiter returns a `LocalDescription` with `type="offer"`.
3. Stuff a stale `None` into `pc._ice_candidates` to simulate the
   leftover sentinel.
4. `restart_ice()` task — manually emit gathering-complete again.
   Assert the second `LocalDescription` is returned cleanly.
5. Assert `pc._ice_candidates` was emptied on entry (the drain
   ran) and a fresh `None` sits there after the second
   gather-complete.

### Fake-based, trickle form (no `@pytest.mark.native`)

Drives the fake the same way but with `trickle=True`:

1. Create PC, run `create_offer()` once to drive it into a
   "previously emitted SDP" state.
2. Stuff a stale candidate + a stale `None` into
   `pc._ice_candidates`.
3. `pc.restart_ice(trickle=True)` — assert it returns immediately
   after the local-description callback (no gathering-complete
   needed) and that `pc._ice_candidates` was drained before the
   restart fired.
4. Emit one fresh candidate via `emit(CB_LOCAL_CANDIDATE, …)`;
   iterate `pc.ice_candidates()` and assert the iterator yields
   only that fresh candidate (the stale one was drained).
5. Repeat the same assertion path via the W3C alias
   `pc.set_local_description(None)` — confirms the two entry
   points are functionally identical.

### Native (`@pytest.mark.native + @pytest.mark.host_only`)

Real libdatachannel loopback pair:

1. Two PCs, A and B. A creates a DataChannel, exchanges
   offer/answer with B via `create_offer`/`set_remote_description`
   in both directions.
2. `await pc_a.wait_for_state(RTCState.CONNECTED)` — confirms the
   handshake completed and ICE picked a candidate pair.
3. Capture `ufrag1` / `pwd1` from `pc_a.get_local_description()`.
4. `await pc_a.restart_ice()`. Forward the new SDP to B
   (`set_remote_description`), have B answer, forward answer back
   to A (`set_remote_description` on A).
5. Assert the new SDP has different `ufrag` / `pwd` from the
   original (proves it was a real ICE restart, not a no-op).
6. Send a message on the DataChannel post-restart; assert it
   arrives. Proves the SCTP layer survived the restart.

The `host_only` marker (introduced for issue #12) keeps this out
of the cibuildwheel manylinux container's hang surface.
`tests-native` on bare-metal Linux + macOS will run it across
Python 3.12 / 3.13 / 3.14.

## Files touched

- `aiolibdatachannel/peer_connection.py` — new `restart_ice` method
  next to `create_offer`; modify `set_local_description` to drain
  the candidate queue + guard on `self._closed`. ~25 lines net.
- `tests/test_restart_ice.py` — new file. Three tests.
- (No change to `aiolibdatachannel/__init__.py`: `PeerConnection`
  is already exported and the new method is reached via the
  instance.)

## Out of scope (deliberate)

- **State-strict guard.** Permissive policy retained (only raises
  on `self._closed`). Matches `create_offer` / `set_local_description`
  and lets libdatachannel surface real misuse at the native
  boundary.
- **A separate `restart_ice_trickle()` method.** The `trickle`
  kwarg covers both shapes from one entry point; an extra method
  would only proliferate API surface.

## Risks

- **Native re-entrancy.** `_native.set_local_description("offer")`
  is called from a coroutine while the previous SDP's callbacks
  may still be in flight (state change on FAILED → CONNECTING).
  libdatachannel's setLocalDescription is documented as safe to
  call from the application thread, and our existing
  `set_local_description` wrapper already does this without
  serialising. No new risk introduced by `restart_ice`.
- **Concurrent calls.** Two coroutines calling `restart_ice`
  simultaneously would both reset the slots and race on the
  callbacks. Same constraint as the existing `create_offer` /
  `create_answer` — single-coroutine usage. Documenting this in
  the doc-string keeps it consistent with the rest of the SDP
  API.
- **Drain in `set_local_description`.** Adding the queue drain
  changes the contract subtly: any consumer that called
  `set_local_description` multiple times in a row and relied on
  the leftover queue contents would see different behaviour.
  Searched the codebase and tests — no such caller exists today,
  and the drain matches the documented `ice_candidates` semantics
  (one stream per SDP generation).
