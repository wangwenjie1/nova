.. -*- rst -*-
.. needs:example_verification
.. needs:body_verification

================================================
 Servers actions (servers, os-instance-actions)
================================================

All users can list available actions for a server.

List Actions For Server
=======================

.. rest_method:: GET /servers/{server_id}/os-instance-actions

Lists actions for a server.

Policy defaults enable only users with the administrative role or the owner of
the server to perform this operation. Cloud providers can change these permissions
through the ``policy.json`` file.

Normal response codes: 200

Error response codes: unauthorized(401), forbidden(403), itemNotFound(404)

Request
-------

.. rest_parameters:: parameters.yaml


  - server_id: server_id_path

Response
--------

.. rest_parameters:: parameters.yaml


  - instanceActions: instanceActions
  - action: action
  - instance_uuid: instance_id_body
  - message: message
  - project_id: project_id_instance_action
  - request_id: request_id_body
  - start_time: start_time
  - user_id: user_id

**Example List Actions For Server: JSON response**

.. literalinclude:: ../../doc/api_samples/os-instance-actions/instance-actions-list-resp.json
   :language: javascript

Show Server Action Details
==========================

.. rest_method:: GET /servers/{server_id}/os-instance-actions/{request_id}

Shows details for a server action.

Policy defaults enable only users with the administrative role to perform this operation.
Cloud providers can change these permissions through the ``policy.json`` file.

Normal response codes: 200

Error response codes: unauthorized(401), forbidden(403), itemNotFound(404)

Request
-------

.. rest_parameters:: parameters.yaml


  - server_id: server_id_path
  - request_id: request_id

Response
--------


.. rest_parameters:: parameters.yaml


  - instanceAction: instanceAction
  - action: action
  - instance_uuid: instance_id_body
  - message: message
  - project_id: project_id_instance_action
  - request_id: request_id_body
  - start_time: start_time
  - user_id: user_id
  - events: instance_action_events
  - events.event: event
  - events.start_time: event_start_time
  - events.finish_time: event_finish_time
  - events.result: event_result
  - events.traceback: event_traceback

**Example Show Server Action Details: JSON response**

.. literalinclude:: ../../doc/api_samples/os-instance-actions/instance-action-get-resp.json
   :language: javascript

