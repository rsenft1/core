import io
import logging
import sys
import time
import traceback

import javabridge
import zmq

from ._pipeline_event_listener import PipelineEventListener
from ..analysis.reply import ImageSetSuccess, ServerExited
from ..analysis.reply import ImageSetSuccessWithDictionary
from ..analysis.reply import NoWork
from ..analysis.request import AnalysisCancel, Display, DisplayPostGroup, OmeroLogin
from ..analysis.request import DebugComplete
from ..analysis.request import DebugWaiting
from ..analysis.request import ExceptionReport
from ..analysis.request import InitialMeasurements
from ..analysis.request import Interaction
from ..analysis.request import MeasurementsReport
from ..analysis.request import PipelinePreferences
from ..analysis.request import SharedDictionary
from ..analysis.request import Work
from ..constants.worker import ED_STOP
from ..constants.worker import NOTIFY_STOP
from ..constants.worker import all_measurements
from ..measurement import Measurements
from ..utilities.java import JAVA_STARTED
from ..utilities.measurement import load_measurements_from_buffer
from ..pipeline import CancelledException
from ..preferences import get_awt_headless
from ..preferences import set_preferences_from_dict
from ..utilities.zmq.communicable.reply.upstream_exit import UpstreamExit
from ..workspace import Workspace


class Worker:
    """An analysis worker processing work at a given address

    """

    def __init__(self, context, analysis_id, work_request_address, keepalive_address, with_stop_run_loop=True):
        from bioformats.formatreader import set_omero_login_hook

        self.context = context
        self.work_request_address = work_request_address
        self.keepalive_address = keepalive_address
        self.cancelled = False
        self.with_stop_run_loop = with_stop_run_loop
        self.current_analysis_id = analysis_id
        self.pipeline = None
        self.preferences = None
        self.initial_measurements = None

        set_omero_login_hook(self.omero_login_handler)

    def __enter__(self):
        # pipeline listener object
        self.pipeline_listener = PipelineEventListener(self.handle_exception)

        # Setup the work server socket
        self.work_socket = self.context.socket(zmq.REQ)
        self.work_socket.connect(self.work_request_address)

        # Establish a connection to the keepalive socket
        self.keepalive_socket = self.context.socket(zmq.SUB)
        # Only listen for STOP events
        self.keepalive_socket.setsockopt(zmq.SUBSCRIBE, b"STOP")
        self.keepalive_socket.connect(self.keepalive_address)

        return self

    def __exit__(self, type, value, traceback):
        if self.initial_measurements is not None:
            self.initial_measurements.close()
        self.initial_measurements = None
        self.keepalive_socket.close()
        self.work_socket.close()

    class AnalysisWorkerThreadObject(object):
        """Provide the scope needed by the analysis worker thread

        """

        def __init__(self, worker):
            self.worker = worker

        def __enter__(self):
            self.worker.enter_thread()

        def __exit__(self, type, value, tb):
            if type is not None:
                traceback.print_exception(type, value, tb)
            self.worker.exit_thread()

    def enter_thread(self):
        if not get_awt_headless():
            javabridge.activate_awt()

    def exit_thread(self):
        from cellprofiler_core.constants.reader import ALL_READERS
        for reader in ALL_READERS.values():
            reader.clear_cached_readers()
        if JAVA_STARTED:
            javabridge.detach()

    def run(self):
        from cellprofiler_core.pipeline.event import CancelledException

        with self.AnalysisWorkerThreadObject(self):
            while not self.cancelled:
                try:
                    logging.debug("Requesting a job")
                    # fetch a job
                    the_request = Work(self.current_analysis_id)
                    job = self.send(the_request)

                    if isinstance(job, NoWork):
                        time.sleep(0.25)  # avoid hammering server
                        # no work, currently.
                        continue
                    self.do_job(job)
                except CancelledException:
                    break

    def do_job(self, job):
        """Handle a work request to its completion

        job - request.Work
        """
        import cellprofiler_core.pipeline as cpp

        job_measurements = []
        try:
            send_dictionary = job.wants_dictionary

            logging.info("Starting job")
            # Fetch the pipeline and preferences for this analysis if we don't have it
            current_pipeline = self.pipeline
            current_preferences = self.preferences
            if not current_pipeline:
                logging.debug("Fetching pipeline and preferences")
                rep = self.send(PipelinePreferences(self.current_analysis_id))
                logging.debug("Received pipeline and preferences response")
                preferences_dict = rep.preferences
                # update preferences to match remote values
                set_preferences_from_dict(preferences_dict)

                logging.debug("Loading pipeline")

                current_pipeline = cpp.Pipeline()
                pipeline_chunks = rep.pipeline_blob.tolist()
                pipeline_io = io.StringIO("".join(pipeline_chunks))
                current_pipeline.loadtxt(pipeline_io, raise_on_error=True)

                logging.debug("Pipeline loaded")
                current_pipeline.add_listener(self.pipeline_listener.handle_event)
                current_preferences = rep.preferences
                self.pipeline = current_pipeline
                self.pipeline.calculate_last_image_uses()
                self.preferences = current_preferences
            else:
                # update preferences to match remote values
                set_preferences_from_dict(current_preferences)

            # Reset the listener's state
            self.pipeline_listener.reset()
            logging.debug("Getting initial measurements")
            # Fetch the path to the intial measurements if needed.

            if self.initial_measurements is None:
                logging.debug("Sending initial measurements request")
                rep = self.send(InitialMeasurements(self.current_analysis_id))
                logging.debug("Got initial measurements")
                self.initial_measurements = load_measurements_from_buffer(rep.buf)
            else:
                logging.debug("Has initial measurements")
            # Make a copy of the measurements for writing during this job
            current_measurements = Measurements(copy=self.initial_measurements)
            all_measurements.add(current_measurements)
            job_measurements.append(current_measurements)

            successful_image_set_numbers = []
            image_set_numbers = job.image_set_numbers
            worker_runs_post_group = job.worker_runs_post_group
            logging.info("Doing job: " + ",".join(map(str, image_set_numbers)))

            self.pipeline_listener.image_set_number = image_set_numbers[0]

            if not worker_runs_post_group:
                # Get the shared state from the first imageset in this run.
                shared_dicts = self.send(
                    SharedDictionary(self.current_analysis_id)
                ).dictionaries
                assert len(shared_dicts) == len(current_pipeline.modules())
                for module, new_dict in zip(current_pipeline.modules(), shared_dicts):
                    module.set_dictionary_for_worker(new_dict)

            # Run prepare group if this is the first image in the group.  We do
            # this here (even if there's no grouping in the pipeline) to ensure
            # that any changes to the modules' shared state dictionaries get
            # propagated correctly.
            should_process = True
            if current_measurements["Image", "Group_Index", image_set_numbers[0]] == 1:
                workspace = Workspace(
                    current_pipeline, None, None, None, current_measurements, None, None
                )
                if not current_pipeline.prepare_group(
                    workspace,
                    current_measurements.get_grouping_keys(),
                    image_set_numbers,
                ):
                    # exception handled elsewhere, possibly cancelling this run.
                    should_process = False
                del workspace

            # process the images
            if should_process:
                abort = False
                for image_set_number in image_set_numbers:
                    try:
                        self.pipeline_listener.image_set_number = image_set_number
                        last_workspace = current_pipeline.run_image_set(
                            current_measurements,
                            image_set_number,
                            self.interaction_handler,
                            self.display_handler,
                            self.cancel_handler,
                        )
                        if self.pipeline_listener.should_abort:
                            abort = True
                            break
                        elif self.pipeline_listener.should_skip:
                            # Report skipped image sets as successful so that
                            # analysis can complete.
                            # Report their measurements because some modules
                            # may have provided measurements before skipping.
                            pass
                        successful_image_set_numbers.append(image_set_number)
                        # Send an indication that the image set finished successfully.
                        if send_dictionary:
                            # The jobserver would like a copy of our modules'
                            # run_state dictionaries.
                            dicts = [
                                m.get_dictionary_for_worker()
                                for m in current_pipeline.modules()
                            ]
                            req = ImageSetSuccessWithDictionary(
                                self.current_analysis_id,
                                image_set_number=image_set_number,
                                shared_dicts=dicts,
                            )
                        else:
                            req = ImageSetSuccess(
                                self.current_analysis_id,
                                image_set_number=image_set_number,
                            )
                        rep = self.send(req)
                    except CancelledException:
                        logging.info("Aborting job after cancellation")
                        abort = True
                    except Exception as e:
                        try:
                            logging.error("Error in pipeline", exc_info=True)
                            if (
                                self.handle_exception(image_set_number=image_set_number)
                                == ED_STOP
                            ):
                                abort = True
                                break
                        except:
                            logging.error(
                                "Error in handling of pipeline exception", exc_info=True
                            )
                            # this is bad.  We can't handle nested exceptions
                            # remotely so we just fail on this run.
                            abort = True

                if abort:
                    current_measurements.close()
                    job_measurements.remove(current_measurements)
                    return

                if worker_runs_post_group:
                    last_workspace.interaction_handler = self.interaction_handler
                    last_workspace.cancel_handler = self.cancel_handler
                    last_workspace.post_group_display_handler = (
                        self.post_group_display_handler
                    )
                    # There might be an exception in this call, but it will be
                    # handled elsewhere, and there's nothing we can do for it
                    # here.
                    current_pipeline.post_group(
                        last_workspace, current_measurements.get_grouping_keys()
                    )
                    del last_workspace

            # send measurements back to server
            req = MeasurementsReport(
                self.current_analysis_id,
                buf=current_measurements.file_contents(),
                image_set_numbers=image_set_numbers,
            )
            rep = self.send(req)

        except CancelledException:
            # Main thread received shutdown signal
            raise

        except Exception:
            logging.error("Error in worker", exc_info=True)
            if self.handle_exception() == ED_STOP:
                raise CancelledException("Cancelling after user-requested stop")
        finally:
            # Clean up any measurements owned by us
            for m in job_measurements:
                m.close()

    def interaction_handler(self, module, *args, **kwargs):
        """handle interaction requests by passing them to the jobserver and wait for the reply."""
        # we write args and kwargs into the InteractionRequest to allow
        # more complex data to be sent by the underlying zmq machinery.
        arg_kwarg_dict = dict(
            [("arg_%d" % idx, v) for idx, v in enumerate(args)]
            + [("kwarg_%s" % name, v) for (name, v) in list(kwargs.items())]
        )
        req = Interaction(
            self.current_analysis_id,
            module_num=module.module_num,
            num_args=len(args),
            kwargs_names=list(kwargs.keys()),
            **arg_kwarg_dict,
        )
        rep = self.send(req)
        return rep.result

    def cancel_handler(self):
        """Handle a cancel request by sending AnalysisCancelRequest

        """
        self.send(AnalysisCancel(self.current_analysis_id))

    def display_handler(self, module, display_data, image_set_number):
        """handle display requests"""
        req = Display(
            self.current_analysis_id,
            module_num=module.module_num,
            display_data_dict=display_data.__dict__,
            image_set_number=image_set_number,
        )
        rep = self.send(req)

    def post_group_display_handler(self, module, display_data, image_set_number):
        req = DisplayPostGroup(
            self.current_analysis_id,
            module.module_num,
            display_data.__dict__,
            image_set_number,
        )
        rep = self.send(req)

    def omero_login_handler(self):
        """Handle requests for an Omero login"""
        from bioformats.formatreader import use_omero_credentials

        req = OmeroLogin(self.current_analysis_id)
        rep = self.send(req)
        use_omero_credentials(rep.credentials)

    def send(self, req, work_socket=None):
        """Send a request and receive a reply

        req - request to send

        socket - socket to use for send. Default is current work socket

        returns a reply on success. If cancelled, throws a CancelledException
        """
        if self.current_analysis_id is None:
            from cellprofiler_core.pipeline.event import CancelledException

            raise CancelledException("Can't send after cancelling")
        if work_socket is None:
            work_socket = self.work_socket
        poller = zmq.Poller()
        poller.register(self.keepalive_socket, zmq.POLLIN)
        poller.register(work_socket, zmq.POLLIN)
        req.send_only(work_socket)
        response = None
        while response is None:
            for socket, state in poller.poll():
                if state != zmq.POLLIN:
                    continue
                elif socket == self.keepalive_socket:
                    notify_msg = self.keepalive_socket.recv()
                    if notify_msg == NOTIFY_STOP:
                        logging.debug("Worker received cancel notification")
                        self.cancelled = True
                        self.raise_cancel(
                            "Received stop notification while waiting for "
                            "response from %s" % str(req)
                        )
                    else:
                        logging.error("Unexpected message on keepalive: " + notify_msg.decode())
                elif socket == work_socket:
                    response = req.recv(work_socket)
        if isinstance(response, (UpstreamExit, ServerExited)):
            self.raise_cancel(
                "Received UpstreamExit for analysis %s during request %s"
                % (self.current_analysis_id, str(req))
            )
        return response

    def raise_cancel(self, msg="Cancelling analysis"):
        """Handle the cleanup after some proximate cause of cancellation

        msg - reason for cancellation

        This should only be called upon detection of a server-driven
        cancellation of analysis: either UpstreamExit or a stop notification
        from the deadman thread.
        """
        from cellprofiler_core.pipeline.event import CancelledException

        logging.debug(msg)
        self.cancelled = True
        if self.initial_measurements is not None:
            self.initial_measurements.close()
        self.initial_measurements = None
        self.pipeline = None
        self.preferences = None
        self.current_analysis_id = None
        raise CancelledException(msg)

    def handle_exception(self, image_set_number=None, module_name=None, exc_info=None):
        """report and handle an exception, possibly by remote debugging, returning
        how to proceed (skip or abort).

        A new socket is created for each exception report, to allow us to sidestep
        any REP/REQ state in the worker.
        """
        if self.current_analysis_id is None:
            # Analysis has been cancelled - don't initiate server interactions
            return ED_STOP
        if exc_info is None:
            t, exc, tb = sys.exc_info()
        else:
            t, exc, tb = exc_info
        filename, line_number, _, _ = traceback.extract_tb(tb, 1)[0]
        report_socket = self.context.socket(zmq.REQ)
        try:
            report_socket.connect(self.work_request_address)
        except:
            return ED_STOP  # nothing to do but give up
        try:
            req = ExceptionReport(
                self.current_analysis_id,
                image_set_number,
                module_name,
                exc_type=t.__name__,
                exc_message=str(exc),
                exc_traceback="".join(traceback.format_exception(t, exc, tb)),
                filename=filename,
                line_number=line_number,
            )
            reply = self.send(req, report_socket)
            while True:
                if reply.disposition == "DEBUG":
                    #
                    # Send DebugWaiting after we know the port #
                    #
                    debug_reply = [None]

                    def pc(port):
                        print("GOT PORT ", port)
                        debug_reply[0] = self.send(
                            DebugWaiting(self.current_analysis_id, port), report_socket,
                        )

                    print("HASH", reply.verification_hash)

                    # We get a new reply at the end, which might be "DEBUG" again.
                    reply = self.send(
                        DebugComplete(self.current_analysis_id), report_socket
                    )
                else:
                    return reply.disposition
        finally:
            report_socket.close()
