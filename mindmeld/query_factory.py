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

"""This module contains the query factory class."""

from __future__ import absolute_import, unicode_literals

from . import ser as sys_ent_rec
from .core import Query, TEXT_FORM_RAW, TEXT_FORM_PROCESSED, TEXT_FORM_NORMALIZED
from .stemmers import EnglishNLTKStemmer
from .tokenizer import Tokenizer


class QueryFactory:
    """An object which encapsulates the components required to create a Query object.

    Attributes:
        preprocessor (Preprocessor): the object responsible for processing raw text
        tokenizer (Tokenizer): the object responsible for normalizing and tokenizing processed
            text
        stemmer (Stemmer): the object responsible for stemming the text
    """
    def __init__(self, tokenizer, preprocessor=None, stemmer=None):
        self.tokenizer = tokenizer
        self.preprocessor = preprocessor
        self.stemmer = stemmer

    def create_query(self, text, time_zone=None, timestamp=None, locale=None, language=None):
        """Creates a query with the given text.

        Args:
            text (str): Text to create a query object for
            time_zone (str, optional): An IANA time zone id to create the query relative to.
            timestamp (int, optional): A reference unix timestamp to create the query relative to,
                in seconds.
            locale (str, optional): The locale representing the ISO 639-1 language code and \
                ISO3166 alpha 2 country code separated by an underscore character.
            language (str, optional): Language as specified using a 639-1/2 code

        Returns:
            Query: A newly constructed query
        """
        raw_text = text

        char_maps = {}

        # create raw, processed maps
        if self.preprocessor:
            processed_text = self.preprocessor.process(raw_text)
            maps = self.preprocessor.get_char_index_map(raw_text, processed_text)
            forward, backward = maps
            char_maps[(TEXT_FORM_RAW, TEXT_FORM_PROCESSED)] = forward
            char_maps[(TEXT_FORM_PROCESSED, TEXT_FORM_RAW)] = backward
        else:
            processed_text = raw_text

        normalized_tokens = self.tokenizer.tokenize(processed_text)
        normalized_text = ' '.join([t['entity'] for t in normalized_tokens])

        # stemmed tokens
        stemmed_tokens = [self.stemmer.stem_word(t['entity']) for t in normalized_tokens]

        # create normalized maps
        maps = self.tokenizer.get_char_index_map(processed_text, normalized_text)
        forward, backward = maps

        char_maps[(TEXT_FORM_PROCESSED, TEXT_FORM_NORMALIZED)] = forward
        char_maps[(TEXT_FORM_NORMALIZED, TEXT_FORM_PROCESSED)] = backward

        query = Query(raw_text, processed_text, normalized_tokens, char_maps, locale=locale,
                      language=language, time_zone=time_zone, timestamp=timestamp,
                      stemmed_tokens=stemmed_tokens)
        query.system_entity_candidates = sys_ent_rec.get_candidates(
            query, locale=locale, language=language)
        return query

    def normalize(self, text):
        """Normalizes the given text.

        Args:
            text (str): Text to process

        Returns:
            str: Normalized text
        """
        return self.tokenizer.normalize(text)

    def __repr__(self):
        return "<{} id: {!r}>".format(self.__class__.__name__, id(self))

    @staticmethod
    def create_query_factory(app_path=None, tokenizer=None, preprocessor=None, stemmer=None):
        """Creates a query factory for the application.

        Args:
            app_path (str, optional): The path to the directory containing the
                app's data. If None is passed, a default query factory will be
                returned.
            tokenizer (Tokenizer, optional): The app's tokenizer. One will be
                created if none is provided
            preprocessor (Processor, optional): The app's preprocessor.
            stemmer (Stemmer, optional): The stemmer to use for stemming

        Returns:
            QueryFactory: A QueryFactory object that is used to create Query objects.
        """
        del app_path
        tokenizer = tokenizer or Tokenizer.create_tokenizer()
        stemmer = stemmer or EnglishNLTKStemmer()
        return QueryFactory(tokenizer, preprocessor, stemmer)
