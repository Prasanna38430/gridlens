{#
    dbt's default appends the custom schema to the target one, so a model
    tagged gridlens_gold would land in gridlens_silver_gold and dbt would try
    to create that database. Terraform owns the production catalog, so a custom
    schema has to be used exactly as written.

    But only on the production target. The first version of this used the
    custom schema verbatim under every target, which meant a ci build of the
    gold models resolved to production gridlens_gold: it would have overwritten
    those tables and appended ci rows to the production run manifest. dbt ls
    showed it on day 19, before anything had been built. Every other target now
    keeps every model in its own schema.
#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is not none and target.name == 'prod' -%}
        {{ custom_schema_name | trim }}
    {%- else -%}
        {{ target.schema }}
    {%- endif -%}
{%- endmacro %}
