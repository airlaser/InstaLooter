# coding: utf-8
"""Instagram looters implementations.
"""
from __future__ import absolute_import
from __future__ import unicode_literals

import abc
import atexit
import itertools
import operator
import os
import random
import sys
import threading
import time
import warnings

import fs
import requests
import six

from fs.base import FS

from . import __author__, __name__ as __appname__, __version__

from ._impl import length_hint
from ._utils import NameGenerator
from .medias import TimedMediasIterator, MediasIterator
from .pages import PageIterator, ProfileIterator, HashtagIterator
from .pbar import ProgressBar
from .worker import InstaDownloader

# mypy annotations
if False:
    from typing import *
    from datetime import datetime
    from queue import Queue
    # type of `timeframe` argument for methods that accept it
    _Timeframe = Tuple[Optional[datetime], Optional[datetime]]


__all__ = [
    "InstaLooter",
    "ProfileLooter",
    "HashtagLooter",
    "PostLooter",
]


@six.add_metaclass(abc.ABCMeta)
class InstaLooter(object):
    """A brutal Instagram looter that raids without API tokens.
    """

    # : The filesystem where cache date is located
    cachefs = fs.open_fs(
        "usercache://{}:{}:{}".format(__appname__, __author__, __version__),
        create=True) # type: FS

    # : The name of the cookie file in the cache filesystem
    _COOKIE_FILE = "cookies.txt"

    @classmethod
    def _init_session(cls, session=None):
        """Initialise the given session and load class cookies to its jar.

        Arguments:
            session (`~requests.Session`): a `requests` session,
                or `None` to create a new one.

        Returns:
            `~requests.Session`: an initialised session instance.

        """
        if session is None:
            session = requests.Session()
        session.cookies = six.moves.http_cookiejar.LWPCookieJar(
            cls.cachefs.getsyspath(cls._COOKIE_FILE))
        try:
            session.cookies.load()
        except IOError:
            pass
        session.cookies.clear_expired_cookies()
        return session

    @classmethod
    def _login(cls, username, password, session=None):
        # type: (str, str, Optional[requests.Session]) -> None
        """Login with provided credentials and session.

        Arguments:
            username (`str`): the username to log in with.
            password (`str`): the password to log in with.
            session (`~requests.Session`): the session to use,
                or `None` to create a new session.

        Note:
            Code taken from LevPasha/instabot.py

        """
        if session is None:
            session = requests.Session()

        homepage = "https://www.instagram.com/"
        data = {'username': username, 'password': password}

        session.headers.update({
            'Origin': homepage,
            'Referer': homepage,
            'X-Instragram-AJAX': '1',
            'X-Requested-With': 'XMLHttpRequest',
        })

        with session.get(homepage) as res:
            session.headers.update({'X-CSRFToken': res.cookies['csrftoken']})
        time.sleep(5 * random.random()) # nosec

        url = "https://www.instagram.com/accounts/login/ajax/"
        with session.post(url, data=data, allow_redirects=True) as res:
            session.headers.update({'X-CSRFToken': res.cookies['csrftoken']})
            if not res.status_code == 200:
                raise SystemError("Login error: check your connection")

        with session.get(url) as res:
            if res.text.find(username) == -1:
                raise ValueError('Login error: check your login data')
            try:
                session.cookies.save()
            except IOError:
                pass

    @classmethod
    def _logout(cls, session=None):
        # type: (Optional[requests.Session]) -> None
        """Log out from current session.

        Also deletes the eventual cookie file left in the cache directory,
        to prevent new connections from using the old session ID.

        Arguments:
            session (`~requests.Session`): the session to use, or `None`
                to create a new session.

        Note:
            Code taken from LevPasha/instabot.py

        """
        if session is None:
            session = requests.Session()

        sessionid = cls._sessionid(session)
        if sessionid is not None:
            url = "https://www.instagram.com/accounts/logout/"
            session.post(url, data={"csrfmiddlewaretoken": sessionid})

        if cls.cachefs.exists(cls._COOKIE_FILE):
            cls.cachefs.remove(cls._COOKIE_FILE)

    @classmethod
    def _logged_in(cls, session=None):
        # type: (Optional[requests.Session]) -> bool
        """Check if there is an open Instagram session.

        Arguments:
            session (`~requests.Session`): the session to use, or `None`
                to create a new session.

        Returns:
            `bool`: `True` if there's an active session, `False` otherwise.

        """
        return cls._sessionid(session) is not None

    @classmethod
    def _sessionid(cls, session=None):
        # type: (Optional[requests.Session]) -> None
        """Get the ID of the currently opened Instagram session.

        Arguments:
            session (`~requests.Session`): the session to use, or `None`
                to create a new session.

        Returns:
            `str` or `None`: the session ID, if any, or `None`.

        """
        session = cls._init_session(session)
        return next((ck.value for ck in session.cookies
                     if ck.domain == "www.instagram.com"
                     and ck.name == "sessionid"
                     and ck.path == "/"), None)

    def __init__(self,
                 add_metadata=False,
                 get_videos=False,
                 videos_only=False,
                 jobs=16,
                 template="{id}",
                 dump_json=False,
                 dump_only=False,
                 extended_dump=False,
                 session=None           # type: Optional[requests.Session]
                 ):
        # type: (...) -> None
        """Create a new looter instance.

        Arguments:
            add_metadata (`bool`): Add date and comment metadata to
                the downloaded pictures.
            get_videos (`bool`): Also get the videos from the given target.
            videos_only (`bool`): Only download videos (implies
                ``get_videos=True``).
            jobs (`bool`): the number of parallel threads to use to
                download media (12 or more is advised to have a true parallel
                download of media files).
            template (`str`): a filename format, in Python new-style-formatting
                format. See the the :ref:`Template` page of the documentation
                for available keys.
            dump_json (`bool`): Save each resource metadata to a
                JSON file next to the actual image/video.
            dump_only (`bool`): Only save metadata and discard the actual
                resource.
            extended_dump (`bool`): Attempt to fetch as much metadata as
                possible, at the cost of more time. Set to `True` if, for
                instance, you always want the top comments to be downloaded
                in the dump.
            session (`~requests.Session`): a `requests` session,
                or `None` to create a new one.

        """
        self.add_metadata = add_metadata
        self.get_videos = get_videos or videos_only
        self.videos_only = videos_only
        self.jobs = jobs
        self.namegen = NameGenerator(template)
        self.dump_only = dump_only
        self.dump_json = dump_json or dump_only
        self.extended_dump = extended_dump
        self.session = self._init_session(session)
        atexit.register(self.session.close)

    @abc.abstractmethod
    def pages(self):
        # type: () -> PageIterator
        """Obtain an iterator over Instagram post pages.

        Returns:
            `PageIterator`: an iterator over the instagram post pages.

        """
        return NotImplemented

    def _medias(self, pages_iterator, timeframe=None):
        # type: (Iterable[dict], Optional[_Timeframe]) -> MediasIterator
        """Obtain an iterator over the medias of the given pages iterator.

        Arguments:
            pages_iterator (`Iterator`): an iterator over the Instagram
                pages, returned by `InstaLooter.pages`

        Returns:
            `MediasIterator`: an iterator over the medias in every pages.

        """
        if timeframe is not None:
            return TimedMediasIterator(pages_iterator, timeframe)
        return MediasIterator(pages_iterator)

    def medias(self, timeframe=None):
        # type: (Optional[_Timeframe]) -> MediasIterator
        """Obtain an iterator over the Instagram medias.

        Wraps the iterator returned by `InstaLooter.pages` to seamlessly
        iterate over the medias of all the pages.

        Returns:
            `MediasIterator`: an iterator over the medias in every pages.

        """
        return self._medias(self.pages(), timeframe)

    def get_post_info(self, code):
        # type: (str) -> dict
        """Get media information from a given post code.

        Arguments:
            code (`str`): the code of the post (can be obtained either
                from the `shortcode` attribute of media dictionaries, or
                from a post URL: ``https://www.instagram.com/p/<code>/``)

        Returns:
            `dict`: a media dictionaries, in the format used by Instagram.

        """
        url = "https://www.instagram.com/p/{}/?__a=1".format(code)
        with self.session.get(url) as res:
            return res.json()['graphql']['shortcode_media']

    def download_pictures(self,
                          destination,       # type: Union[str, FS]
                          media_count=None,  # type: Optional[int]
                          timeframe=None,    # type: Optional[_Timeframe]
                          new_only=False,    # type: bool
                          pgpbar_cls=None,   # type: Optional[Type[ProgressBar]]
                          dlpbar_cls=None    # type: Optional[Type[ProgressBar]]
                          ):
        # type: (...) -> int
        """Download all the pictures to the provided destination.

        Actually a shortcut for `.download` with ``condition`` set
        to accept only images.

        """
        return self.download(
            destination,
            condition=lambda media: not media["is_video"],
            media_count=media_count,
            timeframe=timeframe,
            new_only=new_only,
            pgpbar_cls=pgpbar_cls,
            dlpbar_cls=dlpbar_cls,
        )

    def download_videos(self,
                        destination,       # type: Union[str, FS]
                        media_count=None,  # type: Optional[int]
                        timeframe=None,    # type: Optional[_Timeframe]
                        new_only=False,    # type: bool
                        pgpbar_cls=None,   # type: Optional[Type[ProgressBar]]
                        dlpbar_cls=None,   # type: Optional[Type[ProgressBar]]
                        ):
        # type: (...) -> int
        """Download all videos to the provided destination.

        Actually a shortcut for `.download` with ``condition`` set
        to accept only videos.

        """
        return self.download(
            destination,
            condition=operator.itemgetter("is_video"),
            media_count=media_count,
            timeframe=timeframe,
            new_only=new_only,
            pgpbar_cls=pgpbar_cls,
            dlpbar_cls=dlpbar_cls,
        )

    def download(self,
                 destination,           # type: Union[str, FS]
                 condition=None,        # type: Optional[Callable[[dict], bool]]
                 media_count=None,      # type: Optional[int]
                 timeframe=None,        # type: Optional[_Timeframe]
                 new_only=False,        # type: bool
                 pgpbar_cls=None,       # type: Optional[Type[ProgressBar]]
                 dlpbar_cls=None,       # type: Optional[Type[ProgressBar]]
                 ):
        # type: (...) -> int
        """Download all medias passing ``condition`` to destination.

        Arguments:
            destination (`~fs.base.FS` or str): the filesystem where to store
                the downloaded files, as a filesystem instance or FS URL.
            condition (`Callable`): the condition to filter the
                medias with. If `None` is given, a function is created using
                the ``get_videos`` and ``videos_only`` passed at object
                initialisation.
            media_count (`int`): the maximum number of medias
                to download. Leave to ``None`` to download everything from
                the target. *Note that more files can be downloaded, since
                a post with multiple images/videos is considered to be a
                single media*.
            timeframe (`tuple`): a tuple of two `~datetime.datetime`
                objects to enforce a time frame (the first item must be more
                recent). Leave to `None` to ignore times.
            new_only (`bool`): stop media discovery when already
                downloaded medias are encountered.
            pgpbar_cls (`type`): an optional `.pbar.ProgressBar` subclass
                to use to display page scraping progress.
            dlpbar_cls (`type`): an optional `.pbar.ProgressBar` subclass
                to use to display file download progress.

        Returns:
            `int`: the number of queued medias.

            May not be equal to the number of downloaded medias if some
            errors occurred during background download.

        """
        # Open the destination filesystem
        destination, close_destination = self._init_destfs(destination)

        # Create an iterator over the pages with an optional progress bar
        pages_iterator = self.pages()
        pages_iterator = pgpbar = self._init_pbar(pages_iterator, pgpbar_cls)

        # Create an iterator over the medias
        medias_iterator = self._medias(iter(pages_iterator), timeframe)

        # Create the media download bar from a dummy iterator
        dlpbar = self._init_pbar(
            six.moves.range(length_hint(medias_iterator)), dlpbar_cls)

        # Start a group of workers
        workers, queue = self._init_workers(
            dlpbar if dlpbar_cls is not None else None, destination)

        # Make sure exiting the main thread will shutdown workers
        atexit.register(self._shutdown_workers, workers)

        # Queue all medias
        medias_queued = self._fill_media_queue(
            queue, destination, medias_iterator, media_count,
            new_only, condition)

        # Once queuing the medias is fininished, finish the page progress bar
        # and set a new maximum on the download progress bar.
        if pgpbar_cls is not None:
            pgpbar.finish()
        if dlpbar_cls is not None:
            dlpbar.set_maximum(medias_queued)

        # If no medias were queued, issue a warning
        # TODO: refine warning depending on download parameters
        if medias_queued == 0:
            warnings.warn("No medias found.")

        # Add poison pills to the queue and wait for workers to finish
        self._poison_workers(workers, queue)
        self._join_workers(workers, queue)

        # Once downloading is finished, finish the download progress bar
        # and close the destination if needed.
        if dlpbar_cls is not None:
            dlpbar.finish()
        if close_destination:
            destination.close()

        return medias_queued

    def login(self, username, password):
        # type: (str, str) -> None
        """Log the instance in using the given credentials.

        Arguments:
            username (`str`): the username to log in with.
            password (`str`): the password to log in with.

        """
        self._login(username, password, session=self.session)

    def logout(self):
        # type: () -> None
        """Log the instance out from the currently opened session.
        """
        self._logout(session=self.session)

    def logged_in(self):
        # type: () -> bool
        """Check if there's an open Instagram session.
        """
        return self._logged_in(self.session)

    def _init_pbar(self,
                   it,
                   pbar_cls=None,  # type: Optional[Type[ProgressBar]]
                   ):
        # type: (...) -> Union[ProgressBar, Iterable[Any]]
        """Wrap an iterable within a `ProgressBar`.

        Arguments:
            it (`Iterator`): an iterable to wrap.
            pgpbar_cls (`type`): an optional `ProgressBar` subclass to use,
                or `None` to avoid using a progress bar.

        Returns:
            `Iterable`: the wrapped iterable.

        """
        if pbar_cls is not None:
            if not issubclass(pbar_cls, ProgressBar):
                raise TypeError("pbar must implement the ProgressBar interface !")
            maximum = length_hint(it)
            it = pbar = pbar_cls(it)
            pbar.set_maximum(maximum)
            pbar.set_lock(threading.RLock())
        return it

    def _init_destfs(self, destination, create=True):
        # type: (Union[str, FS], bool) -> Tuple[FS, bool]
        """Open a filesystem either from a FS URL or filesystem instance.

        Arguments:
            destination (`~fs.base.FS` or str): the destination filesystem
                to open, as a filesystem instance or FS URL.
            create (`bool`): whether or not to create a new
                filesystem if it does not exist.

        Returns:
            (`~fs.base.FS`, `bool`): the open FS, and whether to close it.

        """
        close_destination = False
        if isinstance(destination, six.binary_type):
            destination = destination.decode('utf-8')
        if isinstance(destination, six.text_type):
            destination = fs.open_fs(destination, create=create)
            close_destination = True
        if not isinstance(destination, FS):
            raise TypeError("<destination> must be a FS URL or FS instance.")
        return destination, close_destination

    def _fill_media_queue(self,
                          queue,            # type: six.moves.queue.Queue
                          destination,      # type: FS
                          medias_iter,      # type: Iterable[Any]
                          media_count=None, # type: Optional[int]
                          new_only=False,   # type: bool
                          condition=None,   # type: Optional[Callable[[dict], bool]]
                          ):
        # type: (...) -> int
        """Fill the download queue with medias from the provided iterator.

        Arguments:
            queue (`~queue.Queue`): the download queue to fill.
            destination (`~fs.base.FS`): the filesystem where to download
                the files.
            medias_iterator (`Iterable`): an iterable over the Instagram
                medias to download.
            media_count (`int`): the maximum number of new medias to
                download, or ``None`` to download all discoverable medias.
            new_only (`bool`): stop media discovery when a media that was
                already downloaded is encountered.
            condition (`Callable`): the condition to filter the medias with.
                If `None` is given, a function is created using the
                ``get_videos`` and ``videos_only`` passed at object
                initialisation.

        Returns:
            `int`: the number of queued medias.

            May not be equal to the number of downloaded medias if some
            errors occurred during downloads.

        """
        # Create a condition from parameters if needed
        if condition is None:
            if self.videos_only:
                def condition(media): return media['is_video']
            elif not self.get_videos:
                def condition(media): return not media['is_video']
            else:
                def condition(media): return True

        # Queue all media filling the condition
        medias_queued = 0
        for media in six.moves.filter(condition, medias_iter):

            # Check if the whole post info is required
            if self.namegen.needs_extended(media) or media["__typename"] != "GraphImage":
                media = self.get_post_info(media['shortcode'])

            # Check that sidecar children fit the condition
            if media['__typename'] == "GraphSidecar":
                # Check that each node fits the condition
                for sidecar in media['edge_sidecar_to_children']['edges'][:]:
                    if not condition(sidecar['node']):
                        media['edge_sidecar_to_children']['edges'].remove(sidecar)

                # Check that the nodelist is not depleted
                if not media['edge_sidecar_to_children']['edges']:
                    continue

            # Check that the file does not exist
            # FIXME: not working well with sidecar
            if destination.exists(self.namegen.file(media)):
                if new_only:
                    break

            # Put the medias in the queue
            queue.put(media)
            medias_queued += 1

            if media_count is not None and medias_queued >= media_count:
                break

        return medias_queued

    # WORKERS UTILS

    def _init_workers(self,
                      pbar,         # type: Optional[ProgressBar]
                      destination,  # type: FS
                      ):
        # type: (...) -> Tuple[List[InstaDownloader], Queue]

        workers = []                        # type: List[InstaDownloader]
        queue = six.moves.queue.Queue()     # type: Queue

        for _ in six.moves.range(self.jobs):
            worker = InstaDownloader(
                queue=queue,
                destination=destination,
                namegen=self.namegen,
                add_metadata=self.add_metadata,
                dump_json=self.dump_json,
                dump_only=self.dump_only,
                pbar=pbar,
                session=self.session)
            worker.start()
            workers.append(worker)

        return workers, queue

    def _poison_workers(self, workers, queue):
        # type: (List[InstaDownloader], Queue) -> None
        for worker in workers:
            queue.put(None)

    def _join_workers(self, workers, queue):
        # type: (List[InstaDownloader], Queue) -> None
        if any(w.is_alive() for w in workers):
            for worker in workers:
                worker.join()

    def _shutdown_workers(self, workers):
        # type: (List[InstaDownloader]) -> None
        for worker in workers:
            worker.terminate()


class ProfileLooter(InstaLooter):
    """A looter targeting medias on a user profile.
    """

    def __init__(self,
                 username,  # type: str
                 **kwargs
                 ):
        # type: (...) -> None
        super(ProfileLooter, self).__init__(**kwargs)
        self._username = username
        self._owner_id = None

    def pages(self):
        if self._owner_id is None:
            it = ProfileIterator.from_username(self._username, self.session)
            self._owner_id = it.owner_id
            return it
        return ProfileIterator(self._owner_id, self.session)


class HashtagLooter(InstaLooter):
    """A looter targeting medias tagged with a hashtag.
    """

    def __init__(self,
                 hashtag,   # type: str
                 **kwargs
                 ):
        # type: (...) -> None
        super(HashtagLooter, self).__init__(**kwargs)
        self._hashtag = hashtag

    def pages(self):
        return HashtagIterator(self._hashtag, self.session)


class PostLooter(InstaLooter):
    """A looter targeting a specific post.
    """

    def __init__(self,
                 code,      # type: str
                 **kwargs
                 ):
        # type: (...) -> None
        super(PostLooter, self).__init__(**kwargs)
        self.code = code
        self._info = None   # type: Optional[dict]

    @property
    def info(self):
        if self._info is None:
            self._info = self.get_post_info(self.code)
        return self._info

    def pages(self):
        yield {"edge_owner_to_timeline_media": {
            "count": 1,
            "page_info": {
                "has_next_page": False,
                "end_cursor": None,
            },
            "edges": [
                {"node": self.info}
            ],
        }}

    def medias(self, timeframe=None):
        info = self.info
        if timeframe is not None:
            start, end = TimedMediasIterator.get_times(timeframe)
            timestamp = info.get("taken_at_timestamp") or info["media"]
            if not (start >= media_date >= end):
                raise StopIteration
        yield info

    def download(self,
                 destination,       # type: Union[str, FS]
                 condition=None,    # type: Optional[Callable[[dict], bool]]
                 media_count=None,  # type: Optional[int]
                 timeframe=None,    # type: Optional[_Timeframe]
                 new_only=False,    # type: bool
                 pgpbar_cls=None,   # type: Optional[Type[ProgressBar]]
                 dlpbar_cls=None,   # type: Optional[Type[ProgressBar]]
                 ):
        # type: (...) -> int

        destination, close_destination = self._init_destfs(destination)

        queue = six.moves.queue.Queue()
        medias_queued = self._fill_media_queue(
            queue, destination, iter(self.medias()), media_count,
            new_only, condition)
        queue.put(None)

        worker = InstaDownloader(
            queue=queue,
            destination=destination,
            namegen=self.namegen,
            add_metadata=self.add_metadata,
            dump_json=self.dump_json,
            dump_only=self.dump_only,
            pbar=None,
            session=self.session)
        worker.run()

        return medias_queued