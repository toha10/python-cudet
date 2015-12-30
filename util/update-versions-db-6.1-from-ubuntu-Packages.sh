#!/bin/bash

db='../db/versions/6.1/versions.tsv'

#packagesdata="$(wget -O Packages http://mirror.fuel-infra.org/mos/ubuntu/dists/mos6.1/main/binary-amd64/Packages)"
#this is incorrect, because online repo is different from repo in the 6.1 GA ISO!
#need to use Packages file from the ISO only
packagesdata="$(cat Packages)"

lastid=$(tail -n 1 $db | cut -f1)
id=$lastid

echo "$packagesdata" | ./parse-Packages.pl | while read line
do
  fn=$(echo "$line" | cut -f3)
  if [ $(grep -c "$line" $db) -eq 0 ]
  then
    if [ $(grep -c "$fn" $db) -eq 0 ]
    then
      id=$((id+1))
      echo -e "$id\t0\t6.1\trelease\tubuntu\t$line" >> $db
    else
      for n in $(grep -n "	$fn" $db | cut -d: -f1)
      do
        l="$(sed -n "${n}p" $db)"
        part="$(echo "$l" | cut -f-5)"
        sed -i "${n}s/.*/${part}\t${line}/" $db
      done
    fi
  fi
done
