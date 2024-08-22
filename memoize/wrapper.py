"""
[API] Provides an entry point to the library - a wrapper that is used to cache entries.
"""

import asyncio
import datetime
import functools
import logging
from asyncio import Future
from typing import Optional, Callable

from memoize.configuration import CacheConfiguration, NotConfiguredCacheCalledException, \
    DefaultInMemoryCacheConfiguration, MutableCacheConfiguration
from memoize.entry import CacheKey, CacheEntry
from memoize.exceptions import CachedMethodFailedException
from memoize.invalidation import InvalidationSupport
from memoize.statuses import UpdateStatuses


def memoize(method: Optional[Callable] = None, configuration: CacheConfiguration = None,
            invalidation: InvalidationSupport = None, update_status_tracker: Optional[UpdateStatuses] = None):
    """Wraps function with memoization.

    If entry reaches time it should be updated, refresh is performed in background,
    but current entry is still valid and may be returned.
    Once expiration time is reached, refresh is blocking and current entry is considered invalid.

    Note: If wrapped method times out after `method_timeout` (see configuration) the cache will not be populated 
    and a failure occurs.
    
    Note: If wrapped method throws an exception the cache will not be populated and failure occurs.
    
    Note: Failures are indicated by designated exceptions (not original ones).

    To force refreshing immediately upon call to a cached method, set 'force_refresh_memoized' keyword flag, so
    the method will block until it's cache is refreshed.

    Warning: Leaving default configuration is a bad idea as it may not fit your data (may cause OOMs 
    or cache for an inappropriate time).

    :param function method:                         function to be decorated
    :param CacheConfiguration configuration:        cache configuration; default: DefaultInMemoryCacheConfiguration
    :param InvalidationSupport invalidation:        pass created instance of InvalidationSupport to have it configured
    :param UpdateStatuses update_status_tracker:    optional precreated state tracker to allow observability of this state or non-default update lock timeout

    :raises: CachedMethodFailedException            upon call: if cached method timed-out or thrown an exception
    :raises: NotConfiguredCacheCalledException      upon call: if provided configuration is not ready
    """

    if method is None:
        if configuration is None:
            configuration = DefaultInMemoryCacheConfiguration()
        return functools.partial(memoize, configuration=configuration, invalidation=invalidation, update_status_tracker=update_status_tracker)

    if invalidation is not None and not invalidation._initialized() and configuration is not None:
        invalidation._initialize(configuration.storage(), configuration.key_extractor(), method)

    logger = logging.getLogger('{}@{}'.format(memoize.__name__, method.__name__))
    logger.debug('wrapping %s with memoization - configuration: %s', method.__name__, configuration)

    if update_status_tracker is None:
        update_status_tracker = UpdateStatuses()

    async def try_release(key: CacheKey, configuration_snapshot: CacheConfiguration) -> bool:
        if update_status_tracker.is_being_updated(key):
            return False
        try:
            await configuration_snapshot.storage().release(key)
            configuration_snapshot.eviction_strategy().mark_released(key)
            logger.debug('Released cache key %s', key)
            return True
        except Exception as e:
            logger.error('Failed to release cache key %s', key, e)
            return False

    async def refresh(actual_entry: Optional[CacheEntry], key: CacheKey,
                      value_future_provider: Callable[[], asyncio.Future],
                      configuration_snapshot: CacheConfiguration):

        if update_status_tracker.is_being_updated(key):
            if actual_entry is None:
                logger.debug('As no valid entry exists, waiting for results of concurrent refresh %s', key)
                entry = await update_status_tracker.await_updated(key)
                if isinstance(entry, Exception):
                    raise CachedMethodFailedException('Concurrent refresh failed to complete') from entry
                return entry
            else:
                logger.debug('As update point reached but concurrent update already in progress, '
                             'relying on concurrent refresh to finish %s', key)
                return actual_entry

        # below here, is_being_updated was initially false
        try:
            # This future reflects the actual work being done
            value_future = value_future_provider()
        except Exception as e:
            logger.debug('Early failure instantiating coroutine for %s: %s', key, e)
            raise CachedMethodFailedException('Refresh failed to start') from e

        # This future reflects clients being informed of the result, distinct from the actual work
        notification_future = update_status_tracker.mark_being_updated(key)
        try:
            value = await value_future
            offered_entry = configuration_snapshot.entry_builder().build(key, value)
            await configuration_snapshot.storage().offer(key, offered_entry)
            update_status_tracker.mark_updated(key, offered_entry)
            logger.debug('Successfully refreshed cache for key %s', key)

            try:
                eviction_strategy = configuration_snapshot.eviction_strategy()
                eviction_strategy.mark_written(key, offered_entry)
                to_release = eviction_strategy.next_to_release()
                if to_release is not None:
                    asyncio.get_event_loop().call_soon(
                        asyncio.ensure_future,
                        try_release(to_release, configuration_snapshot)
                    )
            except Exception as e:
                logger.error("ignoring failure during eviction after successful refresh", exc_info=e)
            finally:
                return offered_entry
        except asyncio.TimeoutError as e:
            logger.debug('Timeout for %s: %s', key, e)
            update_status_tracker.mark_update_aborted(key, e)
            raise CachedMethodFailedException('Refresh timed out') from e
        except Exception as e:
            logger.debug('Error while refreshing cache for %s: %s', key, e)
            update_status_tracker.mark_update_aborted(key, e)
            raise CachedMethodFailedException('Refresh failed to complete') from e
        finally:
            err = RuntimeError('Attempt to exit refresh with unfinished future')
            if update_status_tracker.is_being_updated(key):
                update_status_tracker.mark_update_aborted(key, err)
            if not notification_future.done():
                notification_future.set_result(err)
                logger.error("Caught attempt to exit refresh for %s with future in unfinished state", key)


    @functools.wraps(method)
    async def wrapper(*args, **kwargs):
        if not configuration.configured():
            raise NotConfiguredCacheCalledException()

        configuration_snapshot = MutableCacheConfiguration.initialized_with(configuration)

        force_refresh = kwargs.pop('force_refresh_memoized', False)
        key = configuration_snapshot.key_extractor().format_key(method, args, kwargs)

        current_entry: Optional[CacheEntry] = await configuration_snapshot.storage().get(key)
        if current_entry is not None:
            configuration_snapshot.eviction_strategy().mark_read(key)

        now = datetime.datetime.now(datetime.timezone.utc)

        def value_future_provider() -> Future:
            # applying timeout to the method call
            return asyncio.ensure_future(asyncio.wait_for(
                method(*args, **kwargs),
                configuration_snapshot.method_timeout().total_seconds()
            ))

        if current_entry is None:
            logger.debug('Creating (blocking) entry for key %s', key)
            result = await refresh(current_entry, key, value_future_provider, configuration_snapshot)
        elif force_refresh:
            logger.debug('Forced entry update (blocking) for key %s', key)
            result = await refresh(current_entry, key, value_future_provider, configuration_snapshot)
        elif current_entry.expires_after <= now:
            logger.debug('Entry expiration reached - entry update (blocking) for key %s', key)
            result = await refresh(None, key, value_future_provider, configuration_snapshot)
        elif current_entry.update_after <= now:
            logger.debug('Entry update point expired - entry update (async - current entry returned) for key %s', key)
            asyncio.get_event_loop().call_soon(
                asyncio.ensure_future,
                refresh(current_entry, key, value_future_provider, configuration_snapshot)
            )
            result = current_entry
        else:
            result = current_entry

        return configuration_snapshot.postprocessing().apply(result.value)

    return wrapper
