# -*- coding: utf-8 -*-
#
# Copyright (c) 2015 Cisco Systems, Inc. and others.  All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module contains the natural language processor.
"""
import os
import sys
from multiprocessing import cpu_count
from concurrent.futures import ProcessPoolExecutor, wait
from abc import ABC, abstractmethod
from copy import deepcopy
import logging
import datetime
import time
import warnings

from .. import path
from ..core import ProcessedQuery, Bunch
from ..exceptions import ProcessorError
from ..resource_loader import ResourceLoader

from .domain_classifier import DomainClassifier
from .intent_classifier import IntentClassifier
from .entity_resolver import EntityResolver, EntityResolverConnectionError
from .entity_recognizer import EntityRecognizer
from .parser import Parser
from .role_classifier import RoleClassifier
from ..path import get_app
from ..exceptions import AllowedNlpClassesKeyError, MindMeldImportError
from ..markup import process_markup, TIME_FORMAT
from ..query_factory import QueryFactory
from ._config import get_nlp_config
from ..system_entity_recognizer import SystemEntityRecognizer

# ignore sklearn DeprecationWarning, https://github.com/scikit-learn/scikit-learn/issues/10449
warnings.filterwarnings(action='ignore', category=DeprecationWarning)

SUBPROCESS_WAIT_TIME = 0.5
default_num_workers = 0
if sys.version_info > (3, 0):
    default_num_workers = cpu_count()+1

logger = logging.getLogger(__name__)
num_workers = int(os.environ.get('MM_SUBPROCESS_COUNT', default_num_workers))
executor = ProcessPoolExecutor(max_workers=num_workers) if num_workers > 0 else None


def restart_subprocesses():
    """Restarts the process pool executor"""
    global executor  # pylint: disable=global-statement
    executor.shutdown(wait=False)
    executor = ProcessPoolExecutor(max_workers=num_workers)


def subproc_call_instance_function(instance_id, func_name, *args, **kwargs):
    """
    A module function used as a trampoline to call an instance function
    from within a long running child process.

    Args:
        instance_id (number): id(inst) of the Processor instance that needs called

    Returns:
        The result of the called function
    """
    try:
        instance = Processor.instance_map[instance_id]
        return getattr(instance, func_name)(*args, **kwargs)
    except Exception:  # pylint: disable=broad-except
        # This subprocess does not have the requested instance.  Shut down and
        # it will be recreated by the parent process with updated instances.
        sys.exit(1)


class Processor(ABC):
    """A generic base class for processing queries through the MindMeld NLP
    components.

    Attributes:
        resource_loader (ResourceLoader): An object which can load resources for the processor.
        dirty (bool): Indicates whether the processor has unsaved changes to
            its models.
        ready (bool): Indicates whether the processor is ready to process
            messages.
    """

    instance_map = {}
    """The map of identity to instance."""

    def __init__(self, app_path, resource_loader=None, config=None):
        """Initializes a processor

        Args:
            app_path (str): The path to the directory containing the app's data
            resource_loader (ResourceLoader): An object which can load resources for the processor
            config (dict): A config object with processor settings (e.g. if to use n-best inference)
        """
        self._app_path = app_path
        self.resource_loader = resource_loader or ResourceLoader.create_resource_loader(app_path)

        self._children = Bunch()
        self.ready = False
        self.dirty = False
        self.name = None
        self._incremental_timestamp = None
        self.config = get_nlp_config(app_path, config)
        Processor.instance_map[id(self)] = self

    def build(self, incremental=False, label_set=None):
        """Builds all the natural language processing models for this processor and its children.

        Args:
            incremental (bool, optional): When ``True``, only build models whose training data or
                configuration has changed since the last build. Defaults to ``False``.
            label_set (string, optional): The label set from which to train all classifiers.
        """
        self._build(incremental=incremental, label_set=label_set)
        # Dumping the model when incremental builds are turned on
        # allows for other models with identical data and configs
        # to use a pre-existing model's results on the same run.
        if incremental:
            self._dump()

        for child in self._children.values():
            # We pass the incremental_timestamp to children processors
            child.incremental_timestamp = self.incremental_timestamp
            child.build(incremental=incremental, label_set=label_set)
            if incremental:
                child.dump()

        self.resource_loader.query_cache.dump()
        self.ready = True
        self.dirty = True

    @property
    def incremental_timestamp(self):
        """The incremental timestamp of this processor (str)."""
        return self._incremental_timestamp

    @incremental_timestamp.setter
    def incremental_timestamp(self, ts):
        self._incremental_timestamp = ts

    @abstractmethod
    def _build(self, incremental=False, label_set=None):
        raise NotImplementedError

    def dump(self):
        """Saves all the natural language processing models for this processor and its children to
        disk."""
        self._dump()

        for child in self._children.values():
            child.dump()

        self.resource_loader.query_cache.dump()
        self.dirty = False

    @abstractmethod
    def _dump(self):
        raise NotImplementedError

    def load(self, incremental_timestamp=None):
        """Loads all the natural language processing models for this processor and its children
        from disk.

        Args:
            incremental_timestamp (str, optional): The incremental timestamp value.
        """
        self._load(incremental_timestamp=incremental_timestamp)

        for child in self._children.values():
            child.load(incremental_timestamp=incremental_timestamp)

        self.ready = True
        self.dirty = False

    @abstractmethod
    def _load(self, incremental_timestamp=None):
        raise NotImplementedError

    def evaluate(self, print_stats=False, label_set=None):
        """Evaluates all the natural language processing models for this processor and its
        children.

        Args:
            print_stats (bool): If true, prints the full stats table. Otherwise prints just
                                the accuracy
            label_set (str, optional): The label set from which to evaluate
                                all classifiers.
        """
        self._evaluate(print_stats, label_set)

        for child in self._children.values():
            child.evaluate(print_stats, label_set=label_set)

        self.resource_loader.query_cache.dump()

    @abstractmethod
    def _evaluate(self, print_stats, label_set="test"):
        raise NotImplementedError

    def _check_ready(self):
        if not self.ready:
            raise ProcessorError('Processor not ready, models must be built or loaded first.')

    def process(self, query_text, allowed_nlp_classes=None, locale=None, language=None,
                time_zone=None, timestamp=None, dynamic_resource=None, verbose=False):
        """Processes the given query using the full hierarchy of natural language processing models \
        trained for this application.

        Args:
            query_text (str, tuple): The raw user text input, or a list of the n-best query \
                transcripts from ASR.
            allowed_nlp_classes (dict, optional): A dictionary of the NLP hierarchy that is \
                selected for NLP analysis. An example: ``{'smart_home': {'close_door': {}}}`` \
                where smart_home is the domain and close_door is the intent.
            locale (str, optional): The locale representing the ISO 639-1 language code and \
                ISO3166 alpha 2 country code separated by an underscore character.
            language (str, optional): Language as specified using a 639-1/2 code
            time_zone (str, optional): The name of an IANA time zone, such as \
                'America/Los_Angeles', or 'Asia/Kolkata' \
                See the [tz database](https://www.iana.org/time-zones) for more information.
            timestamp (long, optional): A unix time stamp for the request (in seconds).
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If True, returns class probabilities along with class \
                prediction.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                 applying the full hierarchy of natural language processing models to the input \
                 query.
        """
        query = self.create_query(
            query_text, language=language, locale=locale, time_zone=time_zone, timestamp=timestamp)
        return self.process_query(query, allowed_nlp_classes, dynamic_resource, verbose).to_dict()

    def process_query(self, query, allowed_nlp_classes=None, dynamic_resource=None, verbose=False):
        """Processes the given query using the full hierarchy of natural language processing models \
        trained for this application.

        Args:
            query (Query, tuple): The user input query, or a list of the n-best transcripts \
                query objects.
            allowed_nlp_classes (dict, optional): A dictionary of the NLP hierarchy that is \
                selected for NLP analysis. An example: ``{'smart_home': {'close_door': {}}}`` \
                where smart_home is the domain and close_door is the intent.
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference \
            verbose (bool, optional): If True, returns class probabilities along with class \
                prediction.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the full hierarchy of natural language processing models to the input \
                query.
        """
        raise NotImplementedError

    def _process_list(self, items, func, *args, **kwargs):
        """Processes a list of items in parallel if possible using the executor.
        Args:
            items (list): Items to process.
            func (str): Function name to call for processing.

        Returns:
            (tuple): Results of the processing.
        """
        if executor:
            try:
                results = list(items)
                future_to_idx_map = {}
                for idx, item in enumerate(items):
                    future = executor.submit(
                        subproc_call_instance_function, id(self),
                        func, item, *args, **kwargs)
                    future_to_idx_map[future] = idx
                tasks = wait(future_to_idx_map, timeout=SUBPROCESS_WAIT_TIME)
                if tasks.not_done:
                    raise Exception()
                for future in tasks.done:
                    item = future.result()
                    item_idx = future_to_idx_map[future]
                    results[item_idx] = item
                return tuple(results)
            except (Exception, SystemExit):  # pylint: disable=broad-except
                # process pool is broken, restart it and process current request in series
                restart_subprocesses()
        # process the list in series
        return tuple([getattr(self, func)(itm, *args, **kwargs) for itm in items])

    def create_query(self, query_text, locale=None, language=None, time_zone=None,
                     timestamp=None):
        """Creates a query with the given text.

        Args:
            query_text (str, list[str]): Text or list of texts to create a query object for.
            locale (str, optional): The locale representing the ISO 639-1 language code and \
                ISO3166 alpha 2 country code separated by an underscore character.
            language (str, optional): Language as specified using a 639-1/2 code.
            time_zone (str, optional): The name of an IANA time zone, such as
                'America/Los_Angeles', or 'Asia/Kolkata'
                See the [tz database](https://www.iana.org/time-zones) for more information.
            timestamp (long, optional): A unix time stamp for the request (in seconds).

        Returns:
            (Query): A newly constructed query or tuple of queries.
        """
        if not query_text:
            query_text = ''
        if isinstance(query_text, (list, tuple)):
            return self._process_list(
                query_text, 'create_query', locale=locale, language=language,
                time_zone=time_zone, timestamp=timestamp)
        return self.resource_loader.query_factory.create_query(
            query_text, language=language, locale=locale, time_zone=time_zone, timestamp=timestamp)

    def __repr__(self):
        msg = '<{} {!r} ready: {!r}, dirty: {!r}>'
        return msg.format(self.__class__.__name__, self.name, self.ready, self.dirty)


class NaturalLanguageProcessor(Processor):
    """The natural language processor is the MindMeld component responsible for understanding
    the user input using a hierarchy of natural language processing models.

    Attributes:
        domain_classifier (DomainClassifier): The domain classifier for this application.
    """

    def __init__(self, app_path, resource_loader=None, config=None):
        """Initializes a natural language processor object

        Args:
            app_path (str): The path to the directory containing the app's data
            resource_loader (ResourceLoader): An object which can load resources for the processor
            config (dict): A config object with processor settings (e.g. if to use n-best
                transcripts)
        """
        super().__init__(app_path, resource_loader, config)
        self._app_path = app_path

        # initialize the system entity recognizer singleton
        SystemEntityRecognizer.get_instance(app_path)

        self.name = app_path
        self._load_custom_features()
        self.domain_classifier = DomainClassifier(self.resource_loader)

        for domain in path.get_domains(self._app_path):
            self._children[domain] = DomainProcessor(app_path, domain, self.resource_loader)

        nbest_transcripts_nlp_classes = self.config.get(
            'resolve_entities_using_nbest_transcripts', {})
        if len(nbest_transcripts_nlp_classes) > 0:
            nbest_transcripts_nlp_classes = self.extract_allowed_intents(
                nbest_transcripts_nlp_classes)

            for domain in nbest_transcripts_nlp_classes:
                for intent in nbest_transcripts_nlp_classes[domain]:
                    self.domains[domain].intents[intent].nbest_transcripts_enabled = True

    def _load_custom_features(self):
        # Load __init__.py so nlp object recognizes custom features in python console
        try:
            get_app(self._app_path)
        except MindMeldImportError:
            pass

    @property
    def domains(self):
        """The domains supported by this application."""
        return self._children

    def _build(self, incremental=False, label_set=None):
        if incremental:
            # During an incremental build, we set the incremental_timestamp for caching
            current_ts = datetime.datetime.fromtimestamp(int(time.time())).strftime(TIME_FORMAT)
            self.incremental_timestamp = current_ts

        if len(self.domains) == 1:
            return

        self.domain_classifier.fit(
            label_set=label_set, incremental_timestamp=self.incremental_timestamp)

    def _dump(self):
        if len(self.domains) == 1:
            return

        model_path, incremental_model_path = path.get_domain_model_paths(
            app_path=self._app_path, timestamp=self.incremental_timestamp)

        self.domain_classifier.dump(model_path, incremental_model_path)

    def _load(self, incremental_timestamp=None):
        if len(self.domains) == 1:
            return

        model_path, incremental_model_path = path.get_domain_model_paths(
            app_path=self._app_path, timestamp=incremental_timestamp)

        self.domain_classifier.load(incremental_model_path if incremental_timestamp else model_path)

    def _evaluate(self, print_stats, label_set=None):
        if len(self.domains) > 1:
            domain_eval = self.domain_classifier.evaluate(label_set=label_set)
            if domain_eval:
                print("Domain classification accuracy: '{}'".format(domain_eval.get_accuracy()))
                if print_stats:
                    domain_eval.print_stats()
            else:
                logger.info("Skipping domain classifier evaluation")

    def _process_domain(self, query, allowed_nlp_classes=None, dynamic_resource=None,
                        verbose=False):
        domain_proba = None

        if len(self.domains) > 1:
            if not allowed_nlp_classes:
                if verbose:
                    # predict_proba() returns sorted list of tuples
                    # ie, [(<class1>, <confidence>), (<class2>, <confidence>),...]
                    domain_proba = self.domain_classifier.predict_proba(
                        query, dynamic_resource=dynamic_resource)
                    # Since domain_proba is sorted by class with highest confidence,
                    # get that as the predicted class
                    return domain_proba[0][0], domain_proba
                else:
                    domain = self.domain_classifier.predict(
                        query, dynamic_resource=dynamic_resource)
                    return domain, None
            else:
                if len(allowed_nlp_classes) == 1:
                    domain = list(allowed_nlp_classes.keys())[0]
                    if verbose:
                        domain_proba = [(domain, 1.0)]
                    return domain, domain_proba
                else:
                    sorted_domains = self.domain_classifier.predict_proba(
                        query, dynamic_resource=dynamic_resource)
                    if verbose:
                        domain_proba = sorted_domains
                    for ordered_domain, _ in sorted_domains:
                        if ordered_domain in allowed_nlp_classes.keys():
                            return ordered_domain, domain_proba

                    raise AllowedNlpClassesKeyError(
                        'Could not find user inputted domain in NLP hierarchy')
        else:
            domain = list(self.domains.keys())[0]
            if verbose:
                domain_proba = [(domain, 1.0)]
            return domain, domain_proba

    def process_query(self, query, allowed_nlp_classes=None, dynamic_resource=None, verbose=False):
        """Processes the given query using the full hierarchy of natural language processing models \
        trained for this application.

        Args:
            query (Query, tuple): The user input query, or a list of the n-best transcripts \
                query objects.
            allowed_nlp_classes (dict, optional): A dictionary of the NLP hierarchy that is \
                selected for NLP analysis. An example: ``{'smart_home': {'close_door': {}}}`` \
                where smart_home is the domain and close_door is the intent. If \
                ``allowed_nlp_classes`` is ``None``, we just use the normal model predict \
                functionality.
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If True, returns class probabilities along with class \
                prediction.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the full hierarchy of natural language processing models to the input \
                query.
        """
        self._check_ready()
        if isinstance(query, (list, tuple)):
            top_query = query[0]
        else:
            top_query = query
        domain, domain_proba = self._process_domain(top_query,
                                                    allowed_nlp_classes=allowed_nlp_classes,
                                                    dynamic_resource=dynamic_resource,
                                                    verbose=verbose)

        allowed_intents = allowed_nlp_classes.get(domain) if allowed_nlp_classes else None

        processed_query = self.domains[domain].process_query(
            query, allowed_intents, dynamic_resource=dynamic_resource, verbose=verbose)
        processed_query.domain = domain
        if domain_proba:
            domain_scores = dict(domain_proba)
            scores = processed_query.confidence or {}
            scores["domains"] = domain_scores
            processed_query.confidence = scores
        return processed_query

    def extract_allowed_intents(self, allowed_intents):
        """This function validates a user inputted list of allowed_intents against the NLP
        hierarchy and construct a hierarchy dictionary as follows: ``{domain: {intent: {}}`` if
        the validation of allowed_intents has passed.

        Args:
            allowed_intents (list): A list of allowable intents in the format "domain.intent". \
                If all intents need to be included, the syntax is "domain.*".

        Returns:
            (dict): A dictionary of NLP hierarchy.
        """
        nlp_components = {}

        for allowed_intent in allowed_intents:
            domain, intent = allowed_intent.split(".")

            if domain not in self.domains.keys():
                raise AllowedNlpClassesKeyError(
                    "Domain: {} is not in the NLP component hierarchy".format(domain))

            if intent != "*" and intent not in self.domains[domain].intents.keys():
                raise AllowedNlpClassesKeyError(
                    "Intent: {} is not in the NLP component hierarchy".format(intent))

            if domain not in nlp_components:
                nlp_components[domain] = {}

            if intent == "*":
                for intent in self.domains[domain].intents.keys():
                    # We initialize to an empty dictionary to extend capability for
                    # entity rules in the future
                    nlp_components[domain][intent] = {}
            else:
                nlp_components[domain][intent] = {}

        return nlp_components

    def inspect(self, markup, domain=None, intent=None, dynamic_resource=None):
        """Inspect the marked up query and print the table of features and weights.

        Args:
            markup (str): The marked up query string.
            domain (str): The gold value for domain classification.
            intent (str): The gold value for intent classification.
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
        """
        if self.resource_loader:
            _, query, _ = process_markup(
                markup, self.resource_loader.query_factory, query_options={})
        else:
            query_factory = QueryFactory.create_query_factory()
            _, query, _ = process_markup(markup, query_factory, query_options={})

        if domain:
            print('Inspecting domain classification')
            domain_inspection = self.domain_classifier.inspect(
                query, domain=domain, dynamic_resource=dynamic_resource)
            print(domain_inspection)
            print('')

        if intent:
            print('Inspecting intent classification')
            domain, _ = self._process_domain(query, dynamic_resource=dynamic_resource)
            intent_inspection = self.domains[domain].inspect(
                query, intent=intent, dynamic_resource=dynamic_resource)
            print(intent_inspection)
            print('')

    def process(self, query_text,   # pylint: disable=arguments-differ
                allowed_nlp_classes=None,
                allowed_intents=None,
                locale=None,
                language=None,
                time_zone=None,
                timestamp=None,
                dynamic_resource=None,
                verbose=False):
        """Processes the given query using the full hierarchy of natural language processing models \
        trained for this application.

        Args:
            query_text (str, tuple): The raw user text input, or a list of the n-best query \
                transcripts from ASR.
            allowed_nlp_classes (dict, optional): A dictionary of the NLP hierarchy that is \
                selected for NLP analysis. An example: ``{'smart_home': {'close_door': {}}}`` \
                where smart_home is the domain and close_door is the intent.
            allowed_intents (list, optional): A list of allowed intents to use for \
                the NLP processing.
            locale (str, optional): The locale representing the ISO 639-1 language code and
                ISO3166 alpha 2 country code separated by an underscore character.
            language (str, optional): Language as specified using a 639-1/2 code.
            time_zone (str, optional): The name of an IANA time zone, such as \
                'America/Los_Angeles', or 'Asia/Kolkata' \
                See the [tz database](https://www.iana.org/time-zones) for more information.
            timestamp (long, optional): A unix time stamp for the request (in seconds).
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If True, returns class probabilities along with class \
                prediction.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the full hierarchy of natural language processing models to the input \
                query.
        """
        if allowed_intents is not None and allowed_nlp_classes is not None:
            raise TypeError("'allowed_intents' and 'allowed_nlp_classes' cannot be used together")
        if allowed_intents:
            allowed_nlp_classes = self.extract_allowed_intents(allowed_intents)

        return super().process(query_text, allowed_nlp_classes=allowed_nlp_classes,
                               language=language, time_zone=time_zone, locale=locale,
                               timestamp=timestamp, dynamic_resource=dynamic_resource,
                               verbose=verbose)


class DomainProcessor(Processor):
    """The domain processor houses the hierarchy of domain-specific natural language processing
    models required for understanding the user input for a particular domain.

    Attributes:
        name (str): The name of the domain.
        intent_classifier (IntentClassifier): The intent classifier for this domain.
    """

    @property
    def intents(self):
        """The intents supported within this domain (dict)."""
        return self._children

    def __init__(self, app_path, domain, resource_loader=None):
        """Initializes a domain processor object

        Args:
            app_path (str): The path to the directory containing the app's data
            domain (str): The name of the domain
            resource_loader (ResourceLoader): An object which can load resources for the processor
        """
        super().__init__(app_path, resource_loader)
        self.name = domain
        self.intent_classifier = IntentClassifier(self.resource_loader, domain)
        for intent in path.get_intents(app_path, domain):
            self._children[intent] = IntentProcessor(app_path, domain, intent,
                                                     self.resource_loader)

    def _build(self, incremental=False, label_set=None):
        if len(self.intents) == 1:
            return
        # train intent model
        self.intent_classifier.fit(
            label_set=label_set, incremental_timestamp=self.incremental_timestamp)

    def _dump(self):
        if len(self.intents) == 1:
            return

        model_path, incremental_model_path = path.get_intent_model_paths(
            self._app_path, domain=self.name, timestamp=self.incremental_timestamp)

        self.intent_classifier.dump(model_path, incremental_model_path=incremental_model_path)

    def _load(self, incremental_timestamp=None):
        if len(self.intents) == 1:
            return

        model_path, incremental_model_path = path.get_intent_model_paths(
            app_path=self._app_path, domain=self.name, timestamp=incremental_timestamp)

        self.intent_classifier.load(incremental_model_path if incremental_timestamp else model_path)

    def _evaluate(self, print_stats, label_set="test"):
        if len(self.intents) > 1:
            intent_eval = self.intent_classifier.evaluate(label_set=label_set)
            if intent_eval:
                print("Intent classification accuracy for the {} domain: {}".format(
                    self.name, intent_eval.get_accuracy()))
                if print_stats:
                    intent_eval.print_stats()
            else:
                logger.info("Skipping intent classifier evaluation for the '%s' domain", self.name)

    def process(self, query_text,  # pylint: disable=arguments-differ
                allowed_nlp_classes=None,
                locale=None,
                language=None,
                time_zone=None, timestamp=None, dynamic_resource=None, verbose=False):
        """Processes the given input text using the hierarchy of natural language processing models \
        trained for this domain.

        Args:
            query_text (str, or list/tuple): The raw user text input, or a list of the n-best \
                query transcripts from ASR.
            allowed_nlp_classes (dict, optional): A dictionary of the intent section of the \
                NLP hierarchy that is selected for NLP analysis. An example: \
                    { \
                        close_door: {} \
                    } \
                where close_door is the intent. The intent belongs to the smart_home domain. \
                If allowed_nlp_classes is None, we use the normal model predict functionality.
            locale (str, optional): The locale representing the ISO 639-1 language code and \
                ISO3166 alpha 2 country code separated by an underscore character.
            language (str, optional): Language as specified using a 639-1/2 code.
            time_zone (str, optional): The name of an IANA time zone, such as \
                'America/Los_Angeles', or 'Asia/Kolkata' \
                See the [tz database](https://www.iana.org/time-zones) for more information.
            timestamp (long, optional): A unix time stamp for the request (in seconds).
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If True, returns class probabilities along with class \
                prediction.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the hierarchy of natural language processing models to the input text.
        """
        query = self.create_query(query_text, time_zone=time_zone, timestamp=timestamp,
                                  language=language, locale=locale)
        processed_query = self.process_query(query, allowed_nlp_classes=allowed_nlp_classes,
                                             dynamic_resource=dynamic_resource, verbose=verbose)
        processed_query.domain = self.name
        return processed_query.to_dict()

    def process_query(self, query, allowed_nlp_classes=None, dynamic_resource=None, verbose=False):
        """Processes the given query using the full hierarchy of natural language processing models \
        trained for this application.

        Args:
            query (Query, or tuple): The user input query, or a list of the n-best transcripts \
                query objects.
            allowed_nlp_classes (dict, optional): A dictionary of the intent section of the \
                NLP hierarchy that is selected for NLP analysis. An example: ``{'close_door': {}}``
                where close_door is the intent. The intent belongs to the smart_home domain. \
                If allowed_nlp_classes is None, we use the normal model predict functionality.
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If True, returns class probabilities along with class \
                prediction.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the full hierarchy of natural language processing models to the input \
                query.
        """
        self._check_ready()

        if isinstance(query, (list, tuple)):
            top_query = query[0]
        else:
            top_query = query

        intent_proba = None
        if len(self.intents) > 1:
            # Check if the user has specified allowed intents
            if not allowed_nlp_classes:
                if verbose:
                    intent_proba = self.intent_classifier.predict_proba(
                        top_query, dynamic_resource=dynamic_resource)
                    intent = intent_proba[0][0]
                else:
                    intent = self.intent_classifier.predict(
                        top_query, dynamic_resource=dynamic_resource)
            else:
                if len(allowed_nlp_classes) == 1:
                    intent = list(allowed_nlp_classes.keys())[0]
                    if verbose:
                        intent_proba = [(intent, 1.0)]
                else:
                    sorted_intents = self.intent_classifier.predict_proba(
                        top_query, dynamic_resource=dynamic_resource)
                    intent = None
                    if verbose:
                        intent_proba = sorted_intents
                    for ordered_intent, _ in sorted_intents:
                        if ordered_intent in allowed_nlp_classes.keys():
                            intent = ordered_intent
                            break

                    if not intent:
                        raise AllowedNlpClassesKeyError(
                            'Could not find user inputted intent in NLP hierarchy')
        else:
            intent = list(self.intents.keys())[0]
            if verbose:
                intent_proba = [(intent, 1.0)]
        processed_query = self.intents[intent].process_query(
            query, dynamic_resource=dynamic_resource, verbose=verbose)
        processed_query.intent = intent
        if intent_proba:
            intent_scores = dict(intent_proba)
            scores = processed_query.confidence or {}
            scores["intents"] = intent_scores
            processed_query.confidence = scores
        return processed_query

    def inspect(self, query, intent=None, dynamic_resource=None):
        """Inspects the query.

        Args:
            query (Query): The query to be predicted.
            intent (str): The expected intent label for this query.
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.

        Returns:
            (DataFrame): The DataFrame that includes every feature, their value, weight and \
             probability
        """
        return self.intent_classifier.inspect(
            query, intent=intent, dynamic_resource=dynamic_resource)


class IntentProcessor(Processor):
    """The intent processor houses the hierarchy of intent-specific natural language processing
    models required for understanding the user input for a particular intent.

    Attributes:
        domain (str): The domain this intent belongs to.
        name (str): The name of this intent.
        entity_recognizer (EntityRecognizer): The entity recognizer for this intent.
    """

    def __init__(self, app_path, domain, intent, resource_loader=None):
        """Initializes an intent processor object

        Args:
            app_path (str): The path to the directory containing the app's data.
            domain (str): The domain this intent belongs to.
            intent (str): The name of this intent.
            resource_loader (ResourceLoader): An object which can load resources for the processor.
        """
        super().__init__(app_path, resource_loader)
        self.domain = domain
        self.name = intent

        self.entity_recognizer = EntityRecognizer(self.resource_loader, domain, intent)
        try:
            self.parser = Parser(self.resource_loader, domain=domain, intent=intent)
        except FileNotFoundError:
            # Unable to load parser config -> no parser
            self.parser = None

        self._nbest_transcripts_enabled = False

    @property
    def entities(self):
        """The entity types associated with this intent (list)."""
        return self._children

    @property
    def nbest_transcripts_enabled(self):
        """Whether or not to run processing on the n-best transcripts for this intent (bool)."""
        return self._nbest_transcripts_enabled

    @nbest_transcripts_enabled.setter
    def nbest_transcripts_enabled(self, value):
        self._nbest_transcripts_enabled = value

    def _build(self, incremental=False, label_set=None):
        """Builds the models for this intent"""

        # train entity recognizer
        self.entity_recognizer.fit(
            label_set=label_set,
            incremental_timestamp=self.incremental_timestamp)

        # Create the entity processors
        entity_types = self.entity_recognizer.entity_types
        for entity_type in entity_types:
            processor = EntityProcessor(self._app_path, self.domain, self.name, entity_type,
                                        self.resource_loader)
            self._children[entity_type] = processor

    def _dump(self):
        model_path, incremental_model_path = path.get_entity_model_paths(
            self._app_path, self.domain, self.name, timestamp=self.incremental_timestamp)

        self.entity_recognizer.dump(model_path, incremental_model_path=incremental_model_path)

    def _load(self, incremental_timestamp=None):
        model_path, incremental_model_path = path.get_entity_model_paths(
            self._app_path, self.domain, self.name, timestamp=incremental_timestamp)
        self.entity_recognizer.load(incremental_model_path if incremental_timestamp else model_path)

        # Create the entity processors
        entity_types = self.entity_recognizer.entity_types
        for entity_type in entity_types:
            processor = EntityProcessor(self._app_path, self.domain, self.name, entity_type,
                                        self.resource_loader)
            self._children[entity_type] = processor

    def _evaluate(self, print_stats, label_set="test"):
        if len(self.entity_recognizer.entity_types) > 1:
            entity_eval = self.entity_recognizer.evaluate(label_set=label_set)
            if entity_eval:
                print("Entity recognition accuracy for the '{}.{}' intent"
                      ": {}".format(self.domain, self.name, entity_eval.get_accuracy()))
                if print_stats:
                    entity_eval.print_stats()
            else:
                logger.info("Skipping entity recognizer evaluation for the '%s.%s' intent",
                            self.domain, self.name)

    def process(self, query_text, locale=None, language=None,  # pylint: disable=arguments-differ
                time_zone=None, timestamp=None, dynamic_resource=None, verbose=False):
        """Processes the given input text using the hierarchy of natural language processing models
        trained for this intent.
        Args:
            query_text (str, list, tuple): The raw user text input, or a list of the n-best query
                transcripts from ASR.
            locale (str, optional): The locale representing the ISO 639-1 language code and \
                ISO3166 alpha 2 country code separated by an underscore character.
            language (str, optional): Language as specified using a 639-1/2 code.
            time_zone (str, optional): The name of an IANA time zone, such as
                'America/Los_Angeles', or 'Asia/Kolkata'
                See the [tz database](https://www.iana.org/time-zones) for more information.
            timestamp (long, optional): A unix time stamp for the request (in seconds).
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If True, returns class as well as predict probabilities.
        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the hierarchy of natural language processing models to the input text.
        """
        query = self.create_query(query_text, time_zone=time_zone, timestamp=timestamp,
                                  language=language, locale=locale)
        processed_query = self.process_query(query, dynamic_resource=dynamic_resource)
        processed_query.domain = self.domain
        processed_query.intent = self.name
        return processed_query.to_dict()

    def _recognize_entities(self, query, dynamic_resource=None, verbose=False):
        """Calls the entity recognition component.

        Args:
            query (Query, tuple): The user input query, or a list of the n-best transcripts
                query objects.
            verbose (bool, optional): If True returns class as well as confidence scores.
        Returns:
            (list): A list of lists of the QueryEntity objects for each transcript.
        """
        if isinstance(query, (list, tuple)):
            if self.nbest_transcripts_enabled:
                nbest_transcripts_entities = self._process_list(
                    query, '_recognize_entities', **{'dynamic_resource': dynamic_resource,
                                                     'verbose': verbose})
                return nbest_transcripts_entities
            else:
                if verbose:
                    return [self.entity_recognizer.predict_proba(
                        query[0], dynamic_resource=dynamic_resource)]
                else:
                    return [self.entity_recognizer.predict(
                        query[0], dynamic_resource=dynamic_resource)]
        if verbose:
            return self.entity_recognizer.predict_proba(
                query, dynamic_resource=dynamic_resource)
        else:
            return self.entity_recognizer.predict(query, dynamic_resource=dynamic_resource)

    def _align_entities(self, entities):
        """If n-best transcripts is enabled, align the spans across transcripts.
        In a single query, there may be multiple entities and multiple entities of the same type.
        Some entities may be misrecognized as another type, entities may fail to be recognized at
        all, entities may be recognized where one doesn't exist, and the span of entities in
        different n-best hypotheses may vary due to mistranscriptions of context words. Taking
        these possibilities into account, we must come up with a method of aligning recognized
        text spans across the n-best transcripts to group them with the other text spans that are
        referring to the same entity.

        Args:
            entities (list of lists of QueryEntity objects): A list of lists of entity objects,
                where each list is the recognized entities for the nth query

        Returns:
            list (of lists of QueryEntity objects): A list of lists of entity objects, where \
                each list is a group of spans that represent the same canonical entity
        """
        # Treat entities and their spans found in the first transcript as global base/reference
        # across all n transcripts
        aligned_entities = [[entity] for entity in entities[0]]
        if len(entities) > 1 and self.nbest_transcripts_enabled:
            for entities_n in entities[1:]:
                index_to_align = 0  # keep track of entities to align
                for entity in entities_n:
                    n_start = entity.span.start
                    n_end = entity.span.end
                    # if span is just one character long, add one to enable some overlap
                    # Eg: '2'
                    if n_start == n_end:
                        n_end += 1
                    # Start from the entities we haven't found an alignment for.
                    # If we found a match with current entity, we wont align the next one
                    # with something before it
                    for j, ref_entity in enumerate(entities[0][index_to_align:]):
                        ref_start = ref_entity.span.start
                        ref_end = ref_entity.span.end
                        if ref_end == ref_start:
                            ref_end += 1
                        # if there is an overlap in spans and is of the same type, align it
                        if min(n_end, ref_end) - max(ref_start, n_start) > 0 and \
                                ref_entity.entity.type == entity.entity.type:
                            index_to_align = index_to_align + j
                            aligned_entities[index_to_align].append(entity)
                            break
        return aligned_entities

    def _classify_and_resolve_entities(self, idx, query, processed_entities, aligned_entities,
                                       verbose=False):
        entity = processed_entities[idx]
        # Run the role classification
        entity, role_confidence = self.entities[entity.entity.type].process_entity(
            query, processed_entities, idx, verbose)
        # Run the entity resolution
        entity = self.entities[entity.entity.type].resolve_entity(entity, aligned_entities[idx])
        return [entity, role_confidence]

    def _process_entities(self, query, entities, aligned_entities, verbose=False):
        """

        Args:
            query (Query, or tuple): The user input query, or a list of the n-best transcripts
                query objects
            entities (list of lists of QueryEntity objects): A list of lists of entity objects,
                where each list is the recognized entities for the nth query
            aligned_entities (list of lists of QueryEntity): A list of lists of entity objects,
                where each list is a group of spans that represent the same canonical entity

        Returns:
            list (QueryEntity): Returns a list of processed entity objects
        """
        if isinstance(query, (list, tuple)):
            query = query[0]

        processed_entities = [deepcopy(e) for e in entities[0]]
        processed_entities_conf = self._process_list([i for i in range(len(processed_entities))],
                                                     '_classify_and_resolve_entities',
                                                     *[query, processed_entities, aligned_entities,
                                                       verbose])
        if processed_entities_conf:
            processed_entities, role_confidence = [list(tup)
                                                   for tup in zip(*processed_entities_conf)]
        else:
            role_confidence = []
        # Run the entity parsing
        processed_entities = self.parser.parse_entities(query, processed_entities) \
            if self.parser else processed_entities
        return processed_entities, role_confidence

    def _get_pred_entities(self, query, dynamic_resource=None, verbose=False):
        entities = self._recognize_entities(query, dynamic_resource=dynamic_resource,
                                            verbose=verbose)
        pred_entities = entities[0]
        entity_confidence = []
        if verbose and len(pred_entities) > 0:
            for entity, score in pred_entities:
                entity_confidence.append({entity.entity.type: score})
            _pred_entities, _ = zip(*pred_entities)
            return entity_confidence, [_pred_entities]
        return entity_confidence, entities

    def process_query(self, query, dynamic_resource=None, verbose=False):
        """Processes the given query using the hierarchy of natural language processing models \
        trained for this intent.

        Args:
            query (Query, tuple): The user input query, or a list of the n-best transcripts \
                query objects.
            dynamic_resource (dict, optional): A dynamic resource to aid NLP inference.
            verbose (bool, optional): If ``True``, returns class as well as predict probabilities.

        Returns:
            (ProcessedQuery): A processed query object that contains the prediction results from \
                applying the hierarchy of natural language processing models to the input query.
        """
        self._check_ready()

        using_nbest_transcripts = False
        if isinstance(query, (list, tuple)):
            if self.nbest_transcripts_enabled:
                using_nbest_transcripts = True
            query = tuple(query)
        else:
            query = (query,)

        entity_confidence, entities = self._get_pred_entities(
            query, dynamic_resource=dynamic_resource, verbose=verbose)

        aligned_entities = self._align_entities(entities)
        processed_entities, role_confidence = self._process_entities(query, entities,
                                                                     aligned_entities, verbose)

        confidence = {'entities': entity_confidence, 'roles': role_confidence} if verbose else {}

        if using_nbest_transcripts:
            return ProcessedQuery(query[0], entities=processed_entities,
                                  confidence=confidence,
                                  nbest_transcripts_queries=query,
                                  nbest_transcripts_entities=entities,
                                  nbest_aligned_entities=aligned_entities)

        return ProcessedQuery(query[0], entities=processed_entities,
                              confidence=confidence)


class EntityProcessor(Processor):
    """The entity processor houses the hierarchy of entity-specific natural language processing
    models required for analyzing a specific entity type in the user input.

    Attributes:
        domain (str): The domain this entity belongs to.
        intent (str): The intent this entity belongs to.
        type (str): The type of this entity.
        name (str): The type of this entity.
        role_classifier (RoleClassifier): The role classifier for this entity type.
    """

    def __init__(self, app_path, domain, intent, entity_type, resource_loader=None):
        """Initializes an entity processor object

        Args:
            app_path (str): The path to the directory containing the app's data.
            domain (str): The domain this entity belongs to.
            intent (str): The intent this entity belongs to.
            entity_type (str): The type of this entity.
            resource_loader (ResourceLoader): An object which can load resources for the processor.
        """
        super().__init__(app_path, resource_loader)
        self.domain = domain
        self.intent = intent
        self.type = entity_type
        self.name = self.type

        self.role_classifier = RoleClassifier(self.resource_loader, domain, intent, entity_type)
        self.entity_resolver = EntityResolver(app_path, self.resource_loader, entity_type)

    def _build(self, incremental=False, label_set=None):
        """Builds the models for this entity type"""
        self.role_classifier.fit(
            label_set=label_set,
            incremental_timestamp=self.incremental_timestamp)
        self.entity_resolver.fit()

    def _dump(self):
        model_path, incremental_model_path = path.get_role_model_paths(
            self._app_path, self.domain, self.intent, self.type,
            timestamp=self.incremental_timestamp)
        self.role_classifier.dump(model_path, incremental_model_path=incremental_model_path)

    def _load(self, incremental_timestamp=None):
        try:
            model_path, incremental_model_path = path.get_role_model_paths(
                self._app_path, self.domain, self.intent, self.type,
                timestamp=incremental_timestamp)
            self.role_classifier.load(incremental_model_path if incremental_timestamp
                                      else model_path)
            self.entity_resolver.load()
        except EntityResolverConnectionError:
            logger.warning('Cannot connect to ES, so Entity Resolver is not loaded.')

    def _evaluate(self, print_stats, label_set="test"):
        if len(self.role_classifier.roles) > 1:
            role_eval = self.role_classifier.evaluate(label_set=label_set)
            if role_eval:
                print("Role classification accuracy for the {}.{}.{}' entity type: {}".format(
                      self.domain, self.intent, self.type, role_eval.get_accuracy()))
                if print_stats:
                    role_eval.print_stats()
            else:
                logger.info("Skipping role classifier evaluation for the '%s.%s.%s' entity type",
                            self.domain, self.intent, self.type)

    def process_entity(self, query, entities, entity_index, verbose=False):
        """Processes the given entity using the hierarchy of natural language processing models \
        trained for this entity type.

        Args:
            query (Query): The query the entity originated from.
            entities (list): All entities recognized in the query.
            entity_index (int): The index of the entity to process.
            verbose (bool): If set to True, returns confidence scores of classes.

        Returns:
            (tuple): Tuple containing: \
                * ProcessedQuery: A processed query object that contains the prediction results \
                     from applying the hierarchy of natural language processing models to the \
                        input entity.
                * confidence_score: confidence scores returned by classifier.
        """
        self._check_ready()
        entity = entities[entity_index]
        confidence_score = None

        if self.role_classifier.roles:
            # Only run role classifier if there are roles!
            if verbose:
                role = self.role_classifier.predict_proba(query, entities, entity_index)
                entity.entity.role = role[0][0]
                confidence_score = dict(role)
            else:
                entity.entity.role = self.role_classifier.predict(query, entities, entity_index)

        return entity, confidence_score

    def resolve_entity(self, entity, aligned_entity_spans=None):
        """Does the resolution of a single entity. If aligned_entity_spans is not None,
        the resolution leverages the n-best transcripts entity spans. Otherwise, it does the
        resolution on just the text of the entity.

        Args:
            entity (QueryEntity): The entity to process.
            aligned_entity_spans (list[QueryEntity]): The list of aligned n-best entity spans
                to improve resolution.

        Returns:
            (Entity): The entity populated with the resolved values.
        """
        self._check_ready()
        if aligned_entity_spans:
            entity_list = [e.entity for e in aligned_entity_spans]
        else:
            entity_list = [entity.entity]
        entity.entity.value = self.entity_resolver.predict(entity_list)
        return entity

    def process_query(self, query, allowed_nlp_classes=None, dynamic_resource=None, verbose=False):
        """Not implemented"""
        del self
        del query
        del allowed_nlp_classes
        del dynamic_resource
        del verbose
        raise NotImplementedError
