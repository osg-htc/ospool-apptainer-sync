# OSPool Apptainer Sync

Syncs container images to the OSPool OSDF/http origin.

A Pegasus workflow creates jobs based on the source type. Some jobs are mapped to the
OSPool, and some are run in a controlled manner on the AP.

Supported images sources are currently:

 - Built Docker images from a public Docker registry 
 - Apptainer definition files 

Image are published under OSDF (`/ospool/uc-shared/public/OSG-Staff/images/v2/`) and
http (https://ospool-images.osgprod.tempest.chtc.io/v2/).

To pull the latest image, first pull `latest.txt` over http, and then the 
resulting image over OSDF.

