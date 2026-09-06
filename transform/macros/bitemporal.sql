{#
    The as_of read, in one place.

    Bronze stores a row per version and nothing else, so answering "what did we
    believe on the 25th" from bronze means a window function over every version
    of every period, every time. The silver versions model turns that into a
    half open interval per version, [known_from, known_to), and the question
    becomes a range predicate that Athena can answer without sorting anything.

    Half open on purpose. A revision learned at exactly t belongs to t, not to
    the instant before it, and closed intervals on both ends would return two
    rows for a key whose version changed at exactly the moment asked about.
#}

{% macro known_at(moment) %}
    known_from <= {{ moment }} and ({{ moment }} < known_to or known_to is null)
{% endmacro %}
