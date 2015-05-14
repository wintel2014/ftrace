#!/usr/bin/python

# Copyright 2015 Huawei Devices USA Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
# Authors:
#       Chuk Orakwue <chuk.orakwue@huawei.com>

try:
    from logbook import Logger
except ImportError:
    import logging
    logging.basicConfig()
    from logging import getLogger as Logger

from collections import defaultdict, namedtuple
from ftrace.interval import Interval, IntervalList
from ftrace.event import EventList
from ftrace.ftrace import register_api, FTraceComponent
from ftrace.composites import sorted_items
from ftrace.utils.decorators import requires, coroutine, memoize
from ftrace.atrace import AtraceTag
from ftrace.common import filter_by_task

log = Logger('Android')

VSYNC = float(1/60.) # 16.67ms

Context = namedtuple('Context', ['pid', 'name', 'interval', 'event'])
Counter = namedtuple('Counter', ['pid', 'name', 'value', 'interval', 'event'])
# For app  launch latency
LaunchLatency = namedtuple('LaunchLatency', ['task', 'interval', 'latency'])
InputLatency = namedtuple('InputLatency', ['interval', 'latency'])

@register_api('android')
class Android(FTraceComponent):
    """
    Class with APIs to process android trace events
    written to the trace buffer. These are events not
    part of ftrace API but useful for monitoring performance of
    various frameworks in android OS. Some events (as of Lollipop) are:

            gfx - Graphics
           input - Input
            view - View System
         webview - WebView
              wm - Window Manager
              am - Activity Manager
            sync - Sync Manager
           audio - Audio
           video - Video
          camera - Camera
             hal - Hardware Modules
             app - Application
             res - Resource Loading
          dalvik - Dalvik VM
              rs - RenderScript
          bionic - Bionic C Library
           power - Power Management

    See `adb shell atrace --list_categories`.

    """
    def __init__(self, trace):
        self._trace = trace
        self._events = trace.events

        self.__event_handlers = {}
        self._tmw_intervals_by_name = defaultdict(IntervalList)

    def _initialize(self):
        self._parse_tmw_events()

    @property
    @requires('tracing_mark_write')
    def event_names(self):
        return set(self._tmw_intervals_by_name.keys())

    @requires('tracing_mark_write')
    @memoize
    def event_intervals(self, name=None, task=None, 
                        interval=None, match_exact=True):
        """Returns event intervals for specified `name` and `task`
        Name here implies `section` or `counter` name.
        """
        if name is None:
            intervals = \
                IntervalList(sorted_items(self._tmw_intervals_by_name.values()))
        else:
            if match_exact:
                intervals = self._tmw_intervals_by_name[name]
            else:
                intervals = IntervalList(sorted_items(value for key, value in
                    self._tmw_intervals_by_name.iteritems() if name in key))

        intervals = intervals.slice(interval=interval)
        if task:
            intervals = filter(lambda it: it.event.task == task, intervals)

        return intervals

    #--------------------------------------------------------------------------
    """
    Utility script to estimate Frame Rate (FPS) and Jank.
    
    Jank = Interval when surfaceFlinger failed to present.
    """
    @requires('tracing_mark_write')
    @memoize
    def framerate(self, interval=None):
        """
        Since SurfaceFlinger(SF) in Android updates the frame-buffer only 
        when there's work to be done. Measuring FPS in traditional sense as
        frames / seconds would be incorrect as time might include intervals
        when no screen updates occurred.
        
        To account for this, we use SF Vsync which is set to 0 when SurfaceFlinger
        has work to do. We accumulate intervals when a framebuffer was posted
        and use this as Frame-rate.
        
        See https://source.android.com/devices/graphics/implement.html
        """
        self._jank_intervals_do_not_use = IntervalList()
        present_time, presented_frames = 0.0, 0.0
        
        for vsync_event in self.event_intervals(name='VSYNC-sf', 
                                                interval=interval):
            duration = vsync_event.interval.duration
            if duration < 2*VSYNC: # skip interval when we had nothing to do.
                present_time += duration
                temp = len(self.event_intervals('postFramebuffer', 
                                                interval=vsync_event.interval))
                presented_frames += temp
                if temp == 0.0:
                    self._jank_intervals_do_not_use.append(vsync_event)
        
        return presented_frames/present_time

    @requires('tracing_mark_write')
    @memoize  
    def jank_intervals(self, interval=None):
        """
        Returns list of intervals when a jank (missed frame) occurred.
        """
        _ = self.framerate(interval=interval)
        return self._jank_intervals_do_not_use
        
    #--------------------------------------------------------------------------
    """
    Utility script to estimate input response latency.

    Inputs (Touch/Key presses) triggers USB HID report or I2C bus interrupt thats
    sent to Linux Kernel and mapped by Input driver to specific event type and
    code as standardized by Linux Input Protocol,defined by OEM-mapping
    in `linux/input.h`

     Next `EventHub` in Android OS layer reads the translated signals by opening
     `evdev` devices for each input device. Then Android's `InputReader`
     component then decodes the input events according to the device class and
     produces a stream of Android input events.

    Finally, Android's `InputReader` sends input events to the `InputDispatcher`
    which forwards them to the appropriate window.

    We define input latency as time from handling IRQ from touch driver (e.g.
    irq/13-fts_touc) to time when you a screen update from SurfaceFlinger
    after `DeliverInputEvent`. Technically speaking, this is referred to as
    'input-to-display' latency.

    IMPORTANT: We do not account for delays from touch till when
    IRQ is triggered by touch device. This is typically low (<5ms)
    depending on HW.

    Further Reading:
    ---------------

    https://source.android.com/devices/input/overview.html
    https://www.kernel.org/doc/Documentation/input/event-codes.txt

    """
    @requires('tracing_mark_write', 'sched_switch', 'sched_wakeup')
    @memoize
    def input_latency(self, irq_name, interval=None):
        """
        Returns input-to-display latencies seen in trace.

        IMPORTANT: Trace must be collected with 'input' and 'view' events.
        """
        try:
            return self._input_latencies.slice(interval=interval)
        except AttributeError:
            return self._input_latency_handler(irq_name=irq_name).\
                        slice(interval=interval)


    def _input_latency_handler(self, irq_name):
        """
        Returns list of all input events
        """
        self._input_latencies = IntervalList()
        all_tasks = self._trace.cpu.task_intervals()
        touch_irqs = IntervalList(filter_by_task(
            all_tasks, 'name', irq_name, 'any'))

        def _input_intervals():
            """
            Generator that yields intervals when discrete input event(s)
            are read & decoded by Android `Input Reader`.

            x__x__x____IR___ID_ID_ID___DI_SU__DI_SU__DI_SU______

            x = multiple input IRQs (multi-touch translated by Android Input Framework)
            IR = Input Reader [read/decodes multiple events @ once]
            ID = Input Dispatch [dispatches each input event]
            DI = Deliver Input [ appropriate window consumes input event ]
            SU = SurfaceFlinger Screen Update due to window handling input event

            """
            last_timestamp = 0.0
            for ir_event in filter_by_task(all_tasks, 'name', 'InputReader', 'any'):
                yield Interval(last_timestamp, ir_event.interval.end)
                last_timestamp = ir_event.interval.end

        for interval in _input_intervals():
            irqs = touch_irqs.slice(interval=interval, trimmed=False)
            # Necessary as we may be interested in different IRQ name
            if irqs:
                # Use first input event within this interval
                start_ts = irqs[0].interval.start
                end_ts = start_ts
                post_ir_interval = Interval(interval.end, self._trace.duration)
                di_events = self.event_intervals(name='deliverInputEvent',
                                                 interval=post_ir_interval)
                                                 
                if di_events:
                    post_di_interval = Interval(di_events[0].interval.start,
                                                self._trace.duration)
    
                    pfb_events = self.event_intervals(name='postFramebuffer',
                                                      interval=post_di_interval)
    
                    if pfb_events:
                        end_ts = pfb_events[0].interval.end
    
                input_interval = Interval(start=start_ts, end=end_ts)
                self._input_latencies.append(InputLatency(interval=input_interval,
                                            latency=input_interval.duration))

        return self._input_latencies

    #---------------------------------------------------------------------------
    """
    Utility script to estimate application launch latency without instrumenting
    each app. This has been well validated and found effective for over 80 top
    apps on Android Lollipop device. Validation was done by visually comparing
    below markers to screen capture of the launched app.

    IMPORTANT: For complex app with `welcome screen` displayed prior to
    user-interactable window e.g. games, this excludes such intervals and
    only captures up to first displayed window.
    Typically GLSurfaces are used post-welcome screen.
    """

    @memoize
    def _launched_app_events(self, interval=None):
        """
        Upon launch, applications goes through 3 states:
                -  process creation (fork from zygote)
                -  bind application
                -  launch (as defined in App Lifecycle on Android OS 
                           i.e. onCreate/onStart etc.)

        We guestimate which app is launched by on 
        bindApplication logged by Android.
        """
        bindApplications = self.event_intervals(name='bindApplication')
        return bindApplications.slice(interval=interval)

    @memoize
    def launched_app_events(self, interval=None):
        """
        First `bindApplication` indicates first (actual) app-launch.
        Note that any single app-launch can initiate launch of other
        processes (hence forks of zygotes and consequent `bindApplication`)
        """
        return self._launched_app_events(interval=interval)

    @memoize
    def _start_launch_time(self, launched_event):
        """
        Start time estimated as first time we ever saw (i.e. scheduled on CPU)
        the launched task.
        """
        if launched_event:
            interval = Interval(0, launched_event.timestamp)
            return self._trace.cpu.task_intervals(task=launched_event.task,
                interval=interval)[0].interval.start

    @requires('tracing_mark_write')
    @memoize
    def _end_launch_time(self, launched_event, next_launched_event=None):
        """
        End time estimated as last `performTraversals`(screen update) that caused
        a  `setTransactionState`.

        setTransactionState() is invoked to inform SurfaceFlinger state of changes
        of the surface; changes can be layer_state_t and Display_state
        (see native/include/private/gui/LayerState.h).

        layer_state_t indicates changes in position/color/depth/size/alpha/crop etc
        Display_state indicates changes in orientation, etc
        """
        end_time = None
        max_end_time = self._start_launch_time(next_launched_event) \
            if next_launched_event else self._trace.duration
        # after launch
        pl_interval = Interval(launched_event.timestamp, max_end_time)
        performTraversals = self.event_intervals(name='performTraversals', 
                                                 task=launched_event.task, 
                                                 interval=pl_interval,
                                                 match_exact=False)
        last_end = max_end_time
        for pt_event in reversed(performTraversals):
            sts_interval = Interval(pt_event.interval.start, last_end)
            sts_events = self.event_intervals(name='setTransactionState', 
                                              interval=sts_interval)
            # ignore 'setTransactionState' due to app close/focus switch
            # by checking 'wmUpdateFocus'
            wmuf_events = self.event_intervals(name='wmUpdateFocus', 
                                              interval=sts_interval)
            if  sts_events and not wmuf_events and sts_interval.end != max_end_time:
                end_time = sts_interval.end
                break             
            last_end = pt_event.interval.start
        
        return end_time


    @requires('tracing_mark_write', 'sched_switch', 'sched_wakeup')
    @memoize
    def app_launch_latencies(self, task=None):
        """Return launch latency seen in trace"""
        launch_latencies = []
        launched_events = list(self.launched_app_events())
        launched_events.append(None)
        
        for curr_app_event, nex_app_event in zip(launched_events, launched_events[1:]):
            event = curr_app_event.event
            next_event = nex_app_event.event if nex_app_event else None
            if task and event.task != task:
                continue
            start_time, end_time = \
                self._start_launch_time(event), self._end_launch_time(event, next_event)
            if (start_time and end_time) is not None:
                launch_interval = Interval(start_time, end_time)
                launch_latencies.append(LaunchLatency(task=event.task,
                                        interval=launch_interval,
                                        latency=launch_interval.duration))
        return launch_latencies
    #---------------------------------------------------------------------------

    @coroutine
    def _context_handler(self):
        """
        """
        last_timestamp = 0.0
        last_event = None
        counter_events_by_pid = defaultdict(EventList)

        try:
            while True:
                event = (yield)
                pid = event.task.pid
                tag = event.data.atrace_tag
                if tag is AtraceTag.CONTEXT_BEGIN:
                    counter_events_by_pid[pid].append(event)
                elif tag is AtraceTag.CONTEXT_END and counter_events_by_pid[pid]:
                    last_event = counter_events_by_pid[pid].pop()
                    last_timestamp = last_event.timestamp
                    last_pid, last_name = \
                        last_event.data.pid, last_event.data.section_name
                    interval = Interval(last_timestamp, event.timestamp)
                    context = Context(pid=last_pid, name=last_name,
                                      interval=interval, event=last_event)
                    self._tmw_intervals_by_name[last_name].append(context)
                else:
                    log.warn("Missing start marker {event}".format(event=event))

        except GeneratorExit:
            # close things off
            for pid, event_list in counter_events_by_pid.iteritems():
                for event in event_list:
                    last_timestamp = event.timestamp
                    interval = Interval(last_timestamp, self._trace.duration)
                    if event.data.atrace_tag is not AtraceTag.CONTEXT_END:
                        pid, name = event.data.pid, event.data.section_name
                        context = Context(pid=pid, name=name, interval=interval, event=event)
                        self._tmw_intervals_by_name[name].append(context)


    @coroutine
    def _async_event_handler(self):
        """
        TODO: Track by cookie. This is rarely used!!!
        """
        last_timestamp = 0.0
        last_event = None
        # Stack them like Jason (JSON) 'PID', then 'cookie'
        counter_events_by_cookie = defaultdict(EventList)
        counter_events_by_pid = defaultdict(lambda : counter_events_by_cookie)

        try:
            while True:
                event = (yield)
                pid, cookie = event.data.pid, event.data.cookie
                tag = event.data.atrace_tag
                event_list = counter_events_by_pid[pid][cookie]
                if tag is AtraceTag.ASYNC_BEGIN:
                    event_list.append(event)
                elif tag is AtraceTag.ASYNC_END and event_list:
                    last_event = event_list.pop()
                    last_timestamp = last_event.timestamp
                    interval = Interval(last_timestamp, event.timestamp)
                    context = Context(pid=pid, name=last_event.data.section_name,
                                  interval=interval, event=last_event)
                    self._tmw_intervals_by_name[context.name].append(context)
                else:
                    log.warn("Missing start marker {event}".format(event=event))

        except GeneratorExit:
            # close things off
            for pid, by_name in counter_events_by_pid.iteritems():
                for cookie, event_list in by_name.iteritems():
                    for event in event_list:
                        last_timestamp = event.timestamp
                        interval = Interval(last_timestamp, self._trace.duration)
                        context = Context(pid=pid, name=event.data.section_name,
                                          interval=interval, event=event)
                        self._tmw_intervals_by_name[context.name].append(context)

    @coroutine
    def _counter_handler(self):
        """
        """
        last_timestamp = 0.0
        last_value = -1.0
        last_event = None
        # Stack them like Jason (JSON) 'PID', then 'Counter name'
        counter_events_by_name = defaultdict(EventList)
        counter_events_by_pid = defaultdict(lambda : counter_events_by_name)
        try:
            while True:
                event = (yield)
                pid = event.data.pid
                counter_name = event.data.counter_name
                event_list = counter_events_by_pid[pid][counter_name]
                if event_list:
                    last_event = event_list.pop()
                    last_timestamp = last_event.timestamp
                    last_value = last_event.data.value
                event_list.append(event)
                interval = Interval(last_timestamp, event.timestamp)
                counter = Counter(pid=pid, name=counter_name, event=last_event,
                              value=last_value, interval=interval)
                self._tmw_intervals_by_name[counter.name].append(counter)

        except GeneratorExit:
            # close things off
            for pid, by_name in counter_events_by_pid.iteritems():
                for counter_name, event_list in by_name.iteritems():
                    for event in event_list:
                        last_timestamp = event.timestamp
                        last_value = event.data.value
                        interval = Interval(last_timestamp, self._trace.duration)
                        counter = Counter(pid=pid, name=counter_name, event=event,
                                          value=last_value, interval=interval)
                        self._tmw_intervals_by_name[counter.name].append(counter)

    def _parse_tmw_events(self):
        """Parse tracing_mark_write intervals"""
        context_handler = self._context_handler()
        async_event_handler = self._async_event_handler()
        counter_handler = self._counter_handler()

        _ATRACE_TAG_HANDLERS = {
            AtraceTag.CONTEXT_BEGIN : context_handler,
            AtraceTag.CONTEXT_END : context_handler,
            AtraceTag.ASYNC_BEGIN : async_event_handler,
            AtraceTag.ASYNC_END : async_event_handler,
            AtraceTag.COUNTER : counter_handler,
        }

        def tmw_events_gen():
            filter_func = lambda event: event.tracepoint == 'tracing_mark_write'
            for event in filter(filter_func, self._events):
                    yield event

        for event in tmw_events_gen():
            try:
                handler_func = self.__event_handlers[event.data.atrace_tag]
            except KeyError:
                handler_func = \
                    self.__event_handlers[event.data.atrace_tag] = \
                        _ATRACE_TAG_HANDLERS[event.data.atrace_tag]
               # handler_func.next() # prime the coroutine
            handler_func.send(event)

        # shut down the coroutines (..and we are done!)
        for handler_func in self.__event_handlers.itervalues():
            handler_func.close()