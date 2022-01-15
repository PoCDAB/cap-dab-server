#
#    CFNS - Rijkswaterstaat CIV, Delft © 2021 - 2022 <cfns@rws.nl>
#
#    Copyright 2021 - 2022 Bastiaan Teeuwen <bastiaan@mkcl.nl>
#
#    This file is part of cap-dab-server
#
#    cap-dab-server is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    cap-dab-server is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with cap-dab-server. If not, see <https://www.gnu.org/licenses/>.
#

import logging                              # Logging facilities
import multiprocessing                      # Multiprocessing support (for running data streams in the background)
import os                                   # For creating directories
import time                                 # For sleep support
from dab.audio import DABAudioStream        # DAB audio (DAB/DAB+) stream
from dab.data import DABDataStream          # DAB data (packet mode) stream
from dab.streamscfg import StreamsConfig    # streams.ini config
import utils

logger = logging.getLogger('server.dab')

# Class that manages individual DAB stream threads
class DABStreams():
    def __init__(self, config):
        # Set spawn instead of fork, locks up dialog otherwise (TODO find out why)
        multiprocessing.set_start_method('spawn')

        self._srvcfg = config

        self.config = None
        self.streams = []

    def _start_stream(self, stream, index, streamcfg):
        # Create a temporary FIFO for output
        out = utils.create_fifo()

        try:
            if streamcfg['output_type'] == 'data':
                logger.info(f'Starting up DAB data stream {stream}...')
                thread = DABDataStream(self._srvcfg, stream, index, streamcfg, out)
            else:
                logger.info(f'Starting up DAB audio stream {stream}...')

                thread = DABAudioStream(self._srvcfg, stream, index, streamcfg, out)

            thread.start()

            self.streams.insert(index, (stream, thread, streamcfg, out))

            return True
        except KeyError as e:
            logger.error(f'Unable to start DAB stream "{stream}", check configuration. {e}')
        except OSError as e:
            logger.error(f'Unable to start DAB stream "{stream}", invalid streams config. {e}')
        except Exception as e:
            logger.error(f'Unable to start DAB stream "{stream}". {e}')

        self.streams.insert(index, (stream, None, streamcfg, None))

        if out is not None:
            utils.remove_fifo(out)

        return False

    def start(self):
        # Load streams.ini configuration into memory
        self.config = StreamsConfig()
        cfgfile = self._srvcfg['dab']['stream_config']
        if not self.config.load(cfgfile):
            logger.error(f'Unable to load DAB streams configuration: {cfgfile}')
            return False

        # Start all streams one by one
        i = 0
        ret = True
        for stream in self.config.cfg.sections():
            if self._start_stream(stream, i, self.config.cfg[stream]):
                i += 1
            else:
                ret = False

        return ret

    # Get the specified stream's configuration
    def getcfg(self, stream, default=False):
        if default:
            try:
                return self.config.cfg[stream]
            except KeyError:
                return None
        else:
            for s, t, c, o in self.streams:
                if s == stream:
                    return c

            return None

    # Change the configuration for a stream, used for stream replacement mainly
    def setcfg(self, stream, newcfg=None):
        i = 0
        for s, t, c, o in self.streams:
            # Get the current stream
            if s == stream and c is not None:
                # Check if this stream is an audio stream
                if c['output_type'] == 'data':
                    return

                # Restore to the original stream
                if newcfg is None:
                    newcfg = self.config.cfg[stream]

                # Stop the old stream
                del self.streams[i]
                if t is not None:
                    t.join()

                    # Attempt terminating if joining wasn't successful (in case of a process)
                    if t.is_alive() and isinstance(t, multiprocessing.Process):
                        t.terminate()

                        # A last resort
                        if t.is_alive():
                            t.kill()

                    # Allow sockets some time to unbind (FIXME needed?)
                    time.sleep(4)

                # And fire up the new one
                return self._start_stream(stream, i, newcfg)

            i += 1


    def stop(self):
        if self.config is None:
            return

        for s, t, c, o in self.streams:
            if o is not None:
                utils.remove_fifo(o)

            if t is not None:
                t.join()

                # Attempt terminating if joining wasn't successful (in case of a process)
                if t.is_alive() and isinstance(t, multiprocessing.Process):
                    t.terminate()

                    # A last resort
                    if t.is_alive():
                        t.kill()

        self.streams = []

    def restart(self):
        if self.config is None:
            return False

        # Allow sockets some time to unbind
        time.sleep(4)

        self.stop()
        return self.start()

    def status(self):
        streams = []

        if self.config is not None:
            for s, t, c, o in self.streams:
                streams.append((s, t.is_alive() if t is not None else None))

        return streams
